import os, sys
import yaml
import logging
import click
from voluptuous import Schema
from .defaults import settings
from .validators import SchemaCheck, config_file
from .exceptions import *
from .utils import *
from .indexlist import IndexList
from .snapshotlist import SnapshotList
from .actions import *
from ._version import __version__
from .logtools import LogInfo, Whitelist, Blacklist

try:
    from logging import NullHandler
except ImportError:
    from logging import Handler

    class NullHandler(Handler):
        def emit(self, record):
            pass

CLASS_MAP = {
    'alias' :  Alias,
    'allocation' : Allocation,
    'close' : Close,
    'create_index' : CreateIndex,
    'delete_indices' : DeleteIndices,
    'delete_snapshots' : DeleteSnapshots,
    'forcemerge' : ForceMerge,
    'open' : Open,
    'replicas' : Replicas,
    'restore' : Restore,
    'snapshot' : Snapshot,
}

def process_action(client, config, **kwargs):
    """
    Do the `action` in the configuration dictionary, using the associated args.
    Other necessary args may be passed as keyword arguments

    :arg config: An `action` dictionary.
    """
    logger = logging.getLogger(__name__)
    # Make some placeholder variables here for readability
    logger.debug('Configuration dictionary: {0}'.format(config))
    logger.debug('kwargs: {0}'.format(kwargs))
    action = config['action']
    # This will always have some defaults now, so no need to do the if...
    # # OLD WAY: opts = config['options'] if 'options' in config else {}
    opts = config['options']
    logger.debug('opts: {0}'.format(opts))
    mykwargs = {}

    action_class = CLASS_MAP[action]

    # Add some settings to mykwargs...
    if action == 'delete_indices':
        mykwargs['master_timeout'] = (
            kwargs['master_timeout'] if 'master_timeout' in kwargs else 30)
    if action == 'allocation' or action == 'replicas':
        # Setting the operation timeout to the client timeout
        mykwargs['timeout'] = (
            kwargs['timeout'] if 'timeout' in kwargs else 30)

    ### Update the defaults with whatever came with opts, minus any Nones
    mykwargs.update(prune_nones(opts))
    logger.debug('Action kwargs: {0}'.format(mykwargs))
    # This is no longer necessary with the config schema validator
    # # Verify the args we're going to pass match the action
    # verify_args(action, mykwargs)

    ### Set up the action ###
    if action == 'alias':
        # Special behavior for this action, as it has 2 index lists
        logger.debug('Running "{0}" action'.format(action.upper()))
        action_obj = action_class(**mykwargs)
        if 'add' in config:
            logger.debug('Adding indices to alias "{0}"'.format(opts['name']))
            adds = IndexList(client)
            adds.iterate_filters(config['add'])
            action_obj.add(adds)
        if 'remove' in config:
            logger.debug(
                'Removing indices from alias "{0}"'.format(opts['name']))
            removes = IndexList(client)
            removes.iterate_filters(config['remove'])
            action_obj.remove(removes)
    elif action == 'create_index':
        action_obj = action_class(client, **mykwargs)
    elif action == 'delete_snapshots' or action == 'restore':
        logger.debug('Running "{0}"'.format(action))
        slo = SnapshotList(client, repository=opts['repository'])
        slo.iterate_filters(config)
        # We don't need to send this value to the action
        mykwargs.pop('repository')
        action_obj = action_class(slo, **mykwargs)
    else:
        logger.debug('Running "{0}"'.format(action.upper()))
        ilo = IndexList(client)
        ilo.iterate_filters(config)
        action_obj = action_class(ilo, **mykwargs)
    ### Do the action
    if 'dry_run' in kwargs and kwargs['dry_run'] == True:
        action_obj.do_dry_run()
    else:
        logger.debug('Doing the action here.')
        action_obj.do_action()

@click.command()
@click.option('--config',
    help="Path to configuration file. Default: ~/.curator/curator.yml",
    type=click.Path(exists=True), default=settings.config_file()
)
@click.option('--dry-run', is_flag=True, help='Do not perform any changes.')
@click.argument('action_file', type=click.Path(exists=True), nargs=1)
@click.version_option(version=__version__)
def cli(config, dry_run, action_file):
    """
    Curator for Elasticsearch indices.

    See http://elastic.co/guide/en/elasticsearch/client/curator/current
    """
    # Get config from yaml file
    yaml_config  = get_yaml(config)
    # if the file is empty, which is still valid yaml, set as an empty dict
    yaml_config = {} if not yaml_config else prune_nones(yaml_config)
    # Voluptuous can't verify the schema of a dict if it doesn't have keys,
    # so make sure the keys are at least there and are dict()
    for k in ['client', 'logging']:
        if k not in yaml_config:
            yaml_config[k] = {}
        else:
            yaml_config[k] = prune_nones(yaml_config[k])
    config_dict = SchemaCheck(yaml_config, config_file.client(),
        'Client Configuration', 'full configuration dictionary').result()
    # Set up logging
    log_opts = config_dict['logging']
    loginfo = LogInfo(log_opts)
    logging.root.addHandler(loginfo.handler)
    logging.root.setLevel(loginfo.numeric_log_level)
    logger = logging.getLogger('curator.cli')
    # Set up NullHandler() to handle nested elasticsearch.trace Logger
    # instance in elasticsearch python client
    logging.getLogger('elasticsearch.trace').addHandler(NullHandler())
    if log_opts['blacklist']:
        for bl_entry in ensure_list(log_opts['blacklist']):
            for handler in logging.root.handlers:
                handler.addFilter(Blacklist(bl_entry))

    client_args = config_dict['client']
    test_client_options(client_args)
    logger.debug('Client and logging options validated.')

    # Extract this and save it for later, in case there's no timeout_override.
    default_timeout = client_args.pop('timeout')
    logger.debug('default_timeout = {0}'.format(default_timeout))
    #########################################
    ### Start working on the actions here ###
    #########################################
    action_config = get_yaml(action_file)
    action_dict = validate_actions(action_config)
    actions = action_dict['actions']
    logger.debug('Full list of actions: {0}'.format(actions))
    action_keys = sorted(list(actions.keys()))
    for idx in action_keys:
        action = actions[idx]['action']
        action_disabled = actions[idx]['options'].pop('disable_action')
        logger.debug('action_disabled = {0}'.format(action_disabled))
        continue_if_exception = (
            actions[idx]['options'].pop('continue_if_exception'))
        logger.debug(
            'continue_if_exception = {0}'.format(continue_if_exception))
        timeout_override = actions[idx]['options'].pop('timeout_override')
        logger.debug('timeout_override = {0}'.format(timeout_override))
        ignore_empty_list = actions[idx]['options'].pop('ignore_empty_list')
        logger.debug('ignore_empty_list = {0}'.format(ignore_empty_list))

        ### Skip to next action if 'disabled'
        if action_disabled:
            logger.info(
                'Action ID: {0}: "{1}" not performed because "disable_action" '
                'is set to True'.format(idx, action)
            )
            continue
        else:
            logger.info('Preparing Action ID: {0}, "{1}"'.format(idx, action))
        # Override the timeout, if specified, otherwise use the default.
        if type(timeout_override) == type(int()):
            client_args['timeout'] = timeout_override
        else:
            client_args['timeout'] = default_timeout

        # Set up action kwargs
        kwargs = {}
        kwargs['master_timeout'] = (
            client_args['timeout'] if client_args['timeout'] <= 300 else 300)
        kwargs['dry_run'] = dry_run
        kwargs['timeout'] = client_args['timeout']

        # Create a client object for each action...
        client = get_client(**client_args)
        logger.debug('client is {0}'.format(type(client)))
        ##########################
        ### Process the action ###
        ##########################
        try:
            logger.info('Trying Action ID: {0}, "{1}": '
                '{2}'.format(idx, action, actions[idx]['description'])
            )
            process_action(client, actions[idx], **kwargs)
        except Exception as e:
            if str(type(e)) == "<class 'curator.exceptions.NoIndices'>" or \
                str(type(e)) == "<class 'curator.exceptions.NoSnapshots'>":
                if ignore_empty_list:
                    logger.info(
                        'Skipping action "{0}" due to empty list: '
                        '{1}'.format(action, type(e))
                    )
                else:
                    logger.error(
                        'Unable to complete action "{0}".  No actionable items '
                        'in list: {1}'.format(action, type(e))
                    )
                    sys.exit(1)
            else:
                import traceback
                traceback.print_exc()
                logger.error(
                    'Failed to complete action: {0}.  {1}: '
                    '{2}'.format(action, type(e), e)
                )
                if continue_if_exception:
                    logger.info(
                        'Continuing execution with next action because '
                        '"continue_if_exception" is set to True for action '
                        '{0}'.format(action)
                    )
                else:
                    sys.exit(1)
        logger.info('Action ID: {0}, "{1}" completed.'.format(idx, action))
    logger.info('Job completed.')
