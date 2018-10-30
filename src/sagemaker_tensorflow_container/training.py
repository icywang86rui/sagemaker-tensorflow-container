# Copyright 2018 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the 'License'). You
# may not use this file except in compliance with the License. A copy of
# the License is located at
#
#     http://aws.amazon.com/apache2.0/
#
# or in the 'license' file accompanying this file. This file is
# distributed on an 'AS IS' BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific
# language governing permissions and limitations under the License.

from __future__ import absolute_import

import json
import logging
import subprocess
import time

import sagemaker_containers.beta.framework as framework


logger = logging.getLogger(__name__)


SAGEMAKER_PARAMETER_SERVER_NUM = 'sagemaker_parameter_server_num'


def _is_host_master(hosts, current_host):
    return current_host == hosts[0]


def _build_tf_config(hosts, current_host, ps_num=0, ps_task=False):
    """Builds a dictionary containing cluster information based on number of hosts and number of
    parameter servers.

    Args:
        hosts (list[str]): List of host names in the cluster
        current_host (str): Current host name
        ps_num (int): Number of parameter servers (default: 0)
        ps_task (bool): Set to True if this config is built for a parameter server process
            (default: False)

    Returns:
        dict[str: dict]: A dictionary describing the cluster setup for distributed training.
        For more information regarding TF_CONFIG:
        https://cloud.google.com/ml-engine/docs/tensorflow/distributed-training-details
    """
    # Assign the first host as the master. Rest of the hosts if any will be worker hosts.
    # The first ps_num hosts will also have a parameter task assign to them.
    masters = hosts[:1]
    workers = hosts[1:]
    ps = hosts[:ps_num] if len(hosts) > 1 and ps_num > 0 else None

    def host_addresses(hosts, port=2222):
        return ['{}:{}'.format(host, port) for host in hosts]

    tf_config = {
        "cluster": {
            "master": host_addresses(masters)
        },
        "environment": "cloud"
    }

    if ps:
        tf_config["cluster"]["ps"] = host_addresses(ps, port='2223')

    if workers:
        tf_config["cluster"]["worker"] = host_addresses(workers)

    if ps_task:
        if ps is None:
            raise ValueError(
                'Can not have a ps task if there are no parameter servers in the cluster')
        task_type = 'ps'
        task_index = ps.index(current_host)
    elif _is_host_master(hosts, current_host):
        task_type = 'master'
        task_index = 0
    else:
        task_type = 'worker'
        task_index = workers.index(current_host)

    tf_config["task"] = {"index": task_index, "type": task_type}
    return tf_config


def _env_vars_with_tf_config(env, ps_task):
    env_vars = env.to_env_vars()
    env_vars["TF_CONFIG"] = json.dumps(_build_tf_config(
        hosts=env.hosts,
        current_host=env.current_host,
        ps_num=env.additional_framework_parameters.get(SAGEMAKER_PARAMETER_SERVER_NUM),
        ps_task=ps_task))
    return env_vars


def _run_ps(env):
    env_vars = _env_vars_with_tf_config(env, ps_task=True)
    return framework.modules.run_module(
        env.module_dir, env.to_cmd_args(), env_vars, env.module_name, wait=False)


def _run_worker(env, install_module=False):
    env_vars = _env_vars_with_tf_config(env, ps_task=False)
    if install_module:
        return framework.modules.run_module(
            env.module_dir, env.to_cmd_args(), env_vars, env.module_name)
    else:
        framework.modules.write_env_vars(env_vars)
        framework.modules.run(env.module_name, env.to_cmd_args(), env_vars)


def _should_run_ps_on_this_host(hosts, current_host, parameter_server_num):
    return current_host in hosts[:parameter_server_num]


def _wait_until_master_is_down(master):
    while True:
        try:
            subprocess.check_call(
                ['curl', '{}:2222'.format(master)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            logger.info('master {} is still up, waiting for it to exit'.format(master))
            time.sleep(10)
        except subprocess.CalledProcessError:
            logger.info('master {} is down, stopping parameter server'.format(master))
            return


def train(env):
    """Get training job environment from env and run the training job.

    Args:
        env (sagemaker_containers.beta.framework.env.TrainingEnv): Instance of TrainingEnv class

    Returns:
    """
    parameter_server_num = env.additional_framework_parameters.get(SAGEMAKER_PARAMETER_SERVER_NUM)
    if len(env.hosts) > 1 and parameter_server_num:

        logger.info('Running distributed training job with {} parameter servers'.
                    format(parameter_server_num))
        if _should_run_ps_on_this_host(env.hosts, env.current_host, parameter_server_num):
            logger.info('Launching parameter server process')
            _run_ps(env)
            logger.info('Launching worker process')
            _run_worker(env, install_module=False)
            _wait_until_master_is_down(env.hosts[0])
        else:
            _run_worker(env, install_module=True)

    else:
        framework.modules.run_module(env.module_dir, env.to_cmd_args(),
                                     env.to_env_vars(), env.module_name)


def main():
    """Training entry point
    """
    hyperparameters = framework.env.read_hyperparameters()
    env = framework.training_env(hyperparameters=hyperparameters)
    logger.setLevel(env.log_level)
    train(env)
