import os
import logging

def configure_logging(log_path="", console_level=None, log_level=None):

    console_level = logging.getLevelName(console_level.upper()) if console_level else logging.INFO
    log_level = logging.getLevelName(log_level.upper()) if log_level else logging.NOTSET

    def formatter(target, level = "default"):

        formatters = {
            "file": {
                "default": logging.Formatter('%(asctime)s %(levelname)-8s %(name)-25s: %(message)s', '%m-%d %H:%M')
            },
            "console": {
                "debug": logging.Formatter('%(levelname)-8s %(name)-25s: %(message)s'),
                "default": logging.Formatter('%(message)s')
            }
        }

        try:
            return formatters[target][logging.getLevelName(level).lower()]
        except KeyError:
            return formatters[target]['default']

    def add_logger_handler(logger, filename, loglevel, formatter):

        handler = logging.FileHandler(filename) if filename else logging.StreamHandler()
        handler.setLevel(loglevel)
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    logger = logging.getLogger(None)  # configure root logger
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    progname = os.path.splitext(os.path.basename(__file__))[0]
    log_file = os.path.join(log_path, progname)

    if log_level != logging.NOTSET: add_logger_handler(
        logger,
        "{}.log".format(log_file),
        log_level,
        formatter('file', log_level)
    )

    if console_level != logging.NOTSET: add_logger_handler(
        logger,
        None,
        console_level,
        formatter('console', console_level)
    )