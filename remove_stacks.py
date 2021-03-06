import configparser
from typing import Optional, Dict, Any, List
import boto3
import asyncio
import logging
import shutil
import os


class StackTracker:

    def __init__(self, values: Optional[set or dict]=None):
        """
        Sole purpose is to keep track of stacks so asynchronous tasks have a shared resource to pull
        from.
        """
        self.stacks = set() if not values else values


def check_stack(cf: boto3.client, stack_name: str) -> (Dict[str, Any], bool):
    """
    Check if the stack exists
    :param cf: The cloudformation client from boto3
    :param stack_name: The name to give the stack
    :return: Whether the stack exists or not and the response that was given from cloudformation
    """
    try:
        response = cf.describe_stacks(StackName=stack_name)
    except cf.exceptions.ClientError:
        return {}, False
    return response, True


def create_logger(debug_mode: Optional[bool]=False) -> logging.getLogger:
    """
    Self-explanatory, create a logger for streaming output
    :param debug_mode: Is the developer debugging this or no?
    :return: The logging object.
    """
    logger = logging.getLogger(os.path.basename(__name__))
    logger.setLevel(logging.INFO if not debug_mode else logging.DEBUG)
    formatter = logging.Formatter('%(filename)s:%(funcName)s:%(levelname)s:%(message)s')
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
    return logger


def delete_keys(ec2: boto3.client, keys: List[str]) -> None:
    """
    Delete the ssh keys and the local file of them.
    :param ec2: The ec2 boto client
    :param keys: List of key names to delete from AWS
    :return:
    """
    ssh_root = "ssh_keys"
    shutil.rmtree(ssh_root)
    for key in keys:
        try:
            ec2.delete_key_pair(KeyName=key)
        except ec2.exceptions.ClientError:  # There is already a key for this name
            pass


async def delete_stack(cf: boto3.client, st: StackTracker, stack_name: str, depends_on: List[str]) -> None:
    """
    Delete the given stack.
    :param cf: The cloudformation client
    :param st: Keeps track of which stacks cannot be removed until their children stacks are deleted.
    :param stack_name: The name of the stack to delete
    :param depends_on: The list of parent stacks that can be deleted upon successful deletion of all its children
    :return: Nothing
    """
    exists = True
    while exists:
        if stack_name in st.stacks:
            logger.debug("Stack {} cannot be deleted. Awaiting child stack deletion.".format(stack_name))
            await asyncio.sleep(5)
            continue
        response, exists = check_stack(cf, stack_name)
        if exists:
            if response['Stacks'][0]['StackStatus'] != 'DELETE_IN_PROGRESS':
                logger.info("Deleting {}".format(stack_name))
                cf.delete_stack(StackName=stack_name)
            else:
                await asyncio.sleep(5)
        else:
            for dep in depends_on:
                st.stacks[dep] -= 1
                if st.stacks[dep] <= 0:
                    del st.stacks[dep]
            logger.info("Finished deleting {}".format(stack_name))


def load_aws_creds() -> (str, str, str):
    """
    Load AWS credentials from file
    path: file path to the credential file
    :return: AWS access key and AWS secret key
    """
    session = boto3.Session()
    credentials = session.get_credentials()
    return credentials.access_key, credentials.secret_key, session.region_name


def parse_config_file(path: str, **kwargs) -> List[Dict[str, Any]]:
    """
    Read from some INI file and perform some preliminary operations based on section, if needed.
    :param path: The ini file path
    :param kwargs: Holds the authentication information for AWS
    :return: The information necessary to create a cloudformation stack
    """
    c = configparser.ConfigParser(allow_no_value=True)
    with open(path, "r") as f:
        c.read_file(f)
    config = []
    for section in c.sections():
        depends_on = c.get(section, 'depends_on')
        stack_name = c.get(section, 'name')
        template_path = c.get(section, 'template_path')
        params_path = c.get(section, 'params_path')
        capabilities = c.get(section, 'capabilities')
        if section == 'ec2':
            keys = c.get(section, 'keys')
            keys = [] if not keys else keys.split(',')
            if keys:
                logger.debug("Deleting SSH keys for EC2 instances.")
                ec2 = boto3.client('ec2', **kwargs)
                delete_keys(ec2, keys)
                logger.debug("Finished creating keys.")
        config.append(
            {
                'stack_name': stack_name,
                'template_path': template_path,
                'params_path': None if not params_path else params_path,
                'capabilities': [] if not capabilities else capabilities.split(','),
                'depends_on': [] if not depends_on else depends_on.split(',')
            }
        )
    return config


def main() -> None:
    """
    Remove all stacks that have to do with the corresponding configuration file.
    :return: None
    """
    key, secret, region = load_aws_creds()
    aws_auth = {'aws_access_key_id': key, 'aws_secret_access_key': secret, 'region_name': region}
    cf = boto3.client('cloudformation', **aws_auth)
    config = parse_config_file('stack_config.ini', **aws_auth)
    dependencies = {}
    for c in config:
        for d in c['depends_on']:
            if d not in dependencies:
                dependencies[d] = 0
            else:
                dependencies[d] += 1
    stack_tracker = StackTracker(dependencies)
    loop = asyncio.get_event_loop()
    tasks = [delete_stack(cf, stack_tracker, c['stack_name'], c['depends_on']) for c in config]
    wait_tasks = asyncio.gather(*tasks)
    loop.run_until_complete(wait_tasks)


if __name__ == '__main__':
    logger = create_logger()
    main()
