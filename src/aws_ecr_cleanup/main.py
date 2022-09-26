#!/usr/bin/python3

import os
import re
import sys
from urllib import request
import boto3
import botocore
import datetime
import argparse
import yaml
import jsonpath_ng
import logging
from aws_ecr_cleanup.configure_logging import configure_logging

from dateutil.tz import tzlocal

__version__ = '0.1'

description = """
ECR repository clean up script, version %s
Copyright (c) 2021, Tomasz Habiger, <tomasz.habiger@gmail.com>
""" % __version__

ecr = boto3.client('ecr')
ecs = boto3.client('ecs')
logger = logging.getLogger()

page_size = 100


def protect(func):
    """Decorator for wrapping API calls with try/except blocks and error logging"""

    def execute(**kwargs):

        try:
            logger.debug("Trying to execute %s(%s)" % (func.__name__, kwargs))
            r = func(**kwargs)
            if r['ResponseMetadata']['HTTPStatusCode'] != 200:
                raise Exception("Something went wrong calling API with a %s(%s): %s" % (
                    func.__name__, kwargs, r['ResponseMetadata']))
            return r
        except botocore.exceptions.ClientError as e:
            raise Exception("boto3 client error while executing %s: %s" % (func.__name__, e.__str__()))
        except Exception as e:
            raise Exception("Unexpected error while executing %s: %s" % (func.__name__, e.__str__()))

    return execute


def paginate(func):
    """Decorator for paging the AWS API results"""

    def execute(next_token=None, **kwargs):

        if next_token:
            r = protect(func)(**kwargs, maxResults=page_size, nextToken=next_token)
        else:
            r = protect(func)(**kwargs, maxResults=page_size)

        if 'nextToken' in r:
            return [r] + execute(r['nextToken'], **kwargs)
        else:
            return [r]

    return execute


def chunks(l):
    """A function to split an array into chunks of a defined length. Used to limit the number of arguments
    that script sends to the AWS API."""

    return [l[i:i + page_size] for i in range(0, len(l), page_size)]


class Config:

    def __init__(self, config_file = None):
        """Initialize the object with the data from the file"""
        self.__data = {}

        config_file = config_file if config_file else (os.path.splitext(os.path.basename(__file__))[0]) + ".yaml"

        if os.path.exists(config_file):
            try:
                with open(config_file) as f:
                    self.__data = yaml.load(f, Loader=yaml.FullLoader)
            except Exception as e:
                logger.error("Couldn't open config file: ", e)
        else:
            logger.error("Configuration file doesn't exist!")
            exit(1)

        if self.__data:
            self.__size = len(self.__data)

    def __iter__(self):
        self.n = 0
        return self

    def __next__(self):
        if self.n < self.__size:
            r = self.__data[self.n]
            self.n += 1
            return r
        else:
            raise StopIteration

    @property
    def data(self):
        return self.__data

    def get(self, name=None):
        try:
            return self.__data[name]
        except KeyError:
            return None


class Repository:
    significant_tags = None
    protected_period = 36500
    protected_count = None
    dry_run = True

    def __init__(self, data):
        self.__data = data
        self.__images = None

    def get_images(self):

        a = {
            'repositoryName': self.__data['repositoryName']
        }

        p = paginate(ecr.describe_images)
        images = [item for sublist in p(**a) for item in sublist['imageDetails']]

        return sorted(images, key=lambda x: x['imagePushedAt'], reverse=True)

    @property
    def data(self):
        return self.__data

    # get images in a lazy way, in order to prevent init taking too much time
    @property
    def images(self):
        if not self.__images:
            self.__images = self.get_images()
        return self.__images

    @property
    def tagged(self):
        return [i for i in self.images if 'imageTags' in i]

    def sorted_by(self, field):
        return sorted(self.images, key=lambda x: x[field], reverse=True)

    def filter_by_tags(self, tags):
        return [i for i in self.tagged if set(i['imageTags']) & tags]

    def older_than(self, days):
        d = datetime.datetime.now(tzlocal()) - datetime.timedelta(days=days)
        return [i for i in self.images if i['imagePushedAt'] < d]

    # get the oldest significant image, that is tagged with any of 'significant tags'
    @property
    def oldest_significant(self):
        if Repository.significant_tags:
            significant_images = self.filter_by_tags(Repository.significant_tags)
            try:
                return significant_images.pop()
            except IndexError:
                return None

    def image_index(self, key, value):
        index = None
        for i in range(0, len(self.images)):
            if self.images[i][key] == value:
                index = i
                break
        return index

    def images_to_flush(self):

        if self.oldest_significant:
            try:
                latest_significant_date = self.oldest_significant['imagePushedAt']
                protected_period = (datetime.datetime.now(datetime.timezone.utc) \
                                        - latest_significant_date).days \
                                        + Repository.protected_period
                flush_from_index = max(
                    self.image_index('imageDigest', self.older_than(protected_period).pop(0)['imageDigest']), 
                    self.image_index('imageDigest', self.oldest_significant['imageDigest']) + Repository.protected_count + 1
                )
                return self.images[flush_from_index:]
            except (IndexError, TypeError):
                return None
        else:
            # if no significant image is found, I assuma that all tags are significant
            # and only untagged images beyond the protected period should be flushed
            flush_from_index = self.image_index('imageDigest', Repository.protected_count + 1)
            return [i for i in self.images[flush_from_index:] if not 'imageTags' in i]

    def flush(self):

        images_to_flush = self.images_to_flush()

        if not images_to_flush:
            logger.info("No images met the requirements of cleaning policy")
            return None

        for image_set in chunks(images_to_flush):

            for i in image_set:
                logger.info("%s, %s, %s" % (
                    i['imageDigest'].replace('sha256:', '')[0:11],  # first 11 chars as image ID
                    ", ".join(i.get('imageTags', "")) or "untagged",  # tags
                    i['imagePushedAt'].strftime("%d/%m/%Y %H:%M"))  # push date
                      )
                if image_in_use(i['imageDigest']):
                    logger.error("FATAL: Image is in use! Skipping!")
                    return False

            if not Repository.dry_run:
                protect(ecr.batch_delete_image)(
                    registryId=self.data['registryId'],
                    repositoryName=self.data['repositoryName'],
                    imageIds=[{'imageDigest': i['imageDigest']} for i in image_set]
                )

        if Repository.dry_run:
            logger.info("Running in DRY mode. Not removing anything actually.")


class ECR:
    """Collects ECR repositories objects data"""

    def __init__(self, names):
        """Initialize registry for the repositories names given
        or for all that AWS account is entitled to list."""
        self.__data = {}

        if names:
            self.add(names)
        else:
            self.add_all()

    def get(self, name=None):
        if name:
            try:
                return self.__data[name]
            except KeyError:
                logger.error("Repository %s not found!" % name)
        else:
            return self.__data

    def add(self, names):
        self.__data.update(self.get_repositories(names))

    def add_all(self):
        self.__data = self.get_repositories()

    def get_repositories(self, names = None):

        repositories = []

        if names:
            for n in chunks(names):
                r = protect(ecr.describe_repositories)(repositoryNames=n)
                repositories += r['repositories']
        else:
            repositories = [item for sublist in paginate(ecr.describe_repositories)() for item in
                            sublist['repositories']]

        return {i['repositoryName']: Repository(i) for i in repositories}

    def __len__(self):
        return len(self.__data.keys())

    def __str__(self):
        return "\n".join(self.__data.keys())

    def __iter__(self):
        return self.Iterator(self.__data)

    class Iterator:

        def __init__(self, data):
            self.offset = 0
            self.length = len(data)
            self.items = list(data.items())

        def __next__(self):
            if self.offset >= self.length:
                raise StopIteration
            else:
                item = self.items[self.offset]
                self.offset += 1
                return item


def get_ecs_clusters():
    @paginate
    def __get_ecs_clusters(**kwargs):
        return ecs.list_clusters(**kwargs)

    return [item for sublist in __get_ecs_clusters() for item in sublist['clusterArns']]


def list_ecs_tasks(cluster, flatten=True):
    @paginate
    def __list_ecs_tasks(**kwargs):
        return ecs.list_tasks(cluster=cluster, **kwargs)

    if flatten:
        return [item for sublist in __list_ecs_tasks() for item in sublist['taskArns']]
    else:
        return [item['taskArns'] for item in __list_ecs_tasks()]


def image_currently_in_use_check():
    in_use = {}

    for cluster in get_ecs_clusters():
        for tasks in list_ecs_tasks(cluster, flatten=False):
            if tasks:
                jexpr = jsonpath_ng.parse("$.[*].containers[*].['imageDigest']")
                m = jexpr.find(protect(ecs.describe_tasks)(cluster=cluster, tasks=tasks)['tasks'])
                in_use.update({i.value: i.context.value for i in m})

    def __check(sha):
        return sha in in_use.keys()

    return __check


def parse_args(args):

    def name(astring):
        if re.sub('[a-zA-z0-9_\-\.]+', '', astring):
            raise argparse.ArgumentTypeError
        return astring

    def loglevel(astring):
        return astring.lower()

    parser = argparse.ArgumentParser(
        epilog=description,
    )

    parser.add_argument('--apply', action='store_true', default=False,
                        help='Apply changes. The script will run in dry mode by default.')
    parser.add_argument('--loglevel', nargs='?', type=loglevel, metavar=('string'),
                        default=os.environ.get('LOGLEVEL', 'notset'), choices=['debug', 'info', 'warning', 'critical'],
                        help='File loglevel [info, warning, etc.]. Default: not set')
    parser.add_argument('--console-loglevel', nargs='?', type=loglevel, metavar=('string'),
                        default=os.environ.get('CONSOLE_LOGLEVEL', 'info'),
                        choices=['debug', 'info', 'warning', 'critical'],
                        help='Console loglevel [info, warning, etc.]. Default: info')
    parser.add_argument('--config', nargs='?', type=str, metavar=('string'),
                        required=True, help='Configuration file location.')

    args = parser.parse_args(args)

    return args


def main():
    args = parse_args(sys.argv[1:])

    configure_logging(
        log_level = args.loglevel,
        console_level = args.console_loglevel
    )

    if args.apply:
        Repository.dry_run = False
    else:
        logger.warning("WARNING: Running in DRY mode. To make changes effective, run the script with --apply option.")

   
    for config in Config(config_file=args.config):

        repositories = config.get('repositories')
        protected_repositories = config.get('protected_repositories')

        if protected_repositories and repositories:
            logger.error("ERROR: You cannot use both options `protected_repositories` and `repositories` at the same time.")
            exit(1)

        Repository.significant_tags = set(config.get('significant_tags'))
        Repository.protected_period = config.get('protected_period')
        Repository.protected_count = config.get('protected_count')

        global image_in_use
        image_in_use = image_currently_in_use_check()

        registry = ECR(repositories)

        if registry:
            for (name, _) in registry:
                if protected_repositories and (name in protected_repositories):
                    logger.info("Skiping `%s` as protected repository" % name)
                else:
                    logger.info("Cleaning up `%s` according to the policy: `%s`" % (name, config.get('name')))
                    try:
                        registry.get(name).flush()
                    except AttributeError:
                        logger.error("Unable to flush!")
        else:
            logger.error("Unable to get repositories list form the config file. Nothing to do.")


if __name__ == '__main__':
    main()
