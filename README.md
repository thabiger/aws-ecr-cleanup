### Image Cleanup for Amazon ECR

The script removes stale ECR images. Although ECR offers lifecycle policies that serve a similar task, the logic this script implements cannot be achieved by using them.

The intention is to protect the set of images, that meet the criteria :
- comes from a specified period, counting from the oldest **significant tag**,
- counts no less than N-images, counting from the oldest significant tag (if less than N comes from the specified period).

**Significant tags** are those that are somehow meaningful for the release process, ie. 'dev', 'rc', 'prod', 'stable' and so on. 

As the production/stable images are usually older than their development counterparts, using the oldest tag date as an anchor to calculate the protected period, means protecting some set of images that directly precede the current production release.

### Example

The diagram below explains how script works, assuming: 
- protected period = one year,
- protected count = 13,
- significant tags = dev, rc, prod,
- ecr repositories limited to: hello_world, other_ecr_repo.

![example](https://github.com/thabiger/aws-ecr-cleanup/blob/main/docs/ecr%20cleanup%20policy.png)

By default, it runs in dry mode. To print what images will be removed, run:   

`python3 main.py --config <config_file.yaml>`

with config file content:

```
- name: Flush untagged images older than 7 days, leave no less than 7 images
  protected_period: 7
  protected_count: 7
  significant_tags: '*'
  protected_repositories:
    - some_ignored_repository
- name: Protect images are that created for a year since a last significant tag, leave no less than 13 images
  protected_period: 365
  protected_count: 13
  significant_tags:
    - dev
    - rc
    - prod
    - latest
  repositories:
    - hello_world
``` 

When no repository list is provided, the script will iterate through all of the ECR repositories that are accessible by the AWS account used.
To skip some repositories use `protected_repositories` option.

To actually remove the images, run: 

`python3 main.py --config <config_file.yaml> --apply`