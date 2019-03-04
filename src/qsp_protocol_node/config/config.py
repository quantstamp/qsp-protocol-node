####################################################################################################
#                                                                                                  #
# (c) 2018 Quantstamp, Inc. All rights reserved.  This content shall not be used, copied,          #
# modified, redistributed, or otherwise disseminated except to the extent expressly authorized by  #
# Quantstamp for credentialed users. This content and its use are governed by the Quantstamp       #
# Demonstration License Terms at <https://s3.amazonaws.com/qsp-protocol-license/LICENSE.txt>.      #
#                                                                                                  #
####################################################################################################

"""
Provides the configuration for executing a QSP Audit node,
as loaded from an input YAML file
"""
import importlib
import os
import yaml

from .config_validator import ConfigValidator

from component import BaseConfigComponent
from component import ConfigType

class Config(BaseConfigComponent):

    @classmethod
    def __load_yaml(cls, config_path):
        config_dictionary = None
        with open(config_path) as yaml_file:
            config_dictionary = yaml.load(yaml_file)
            
        return config_dictionary

    @classmethod
    def __get_init_order(cls, factories_dictionary):
        factories_list = [(name, factories_dictionary[name]['init_order']) for name in factories_dictionary]
        factories_list.sort(key=lambda name_order_pair: name_order_pair[1])
        return  [name for (name, _) in factories_list]

    @classmethod
    def create_components(cls, config_obj, tmp_vars, validator=ConfigValidator()):
        try:
            dict.__setitem__(config_obj, 'tmp_vars', tmp_vars)
            print("===> create components. config is {0}".format(config_obj))

            # Loads the config.yaml file as a dictionary
            config_dictionary = Config.__load_yaml(config_obj.tmp_vars['config_file'])
            config_dictionary = config_dictionary[config_obj.tmp_vars['environment']]

            # Loads the factories setting as a dictionary
            factories_path = "{}/factories.yaml".format(
                os.path.dirname(os.path.realpath(__file__))
            )
            factories_dictionary = Config.__load_yaml(factories_path)
            
            ignore_for_now = set(factories_dictionary.keys()).union(config_obj.tmp_vars.keys())
            
            # Makes all attributes (non-components) as properties of this object
            for property_name in set(config_dictionary.keys()) - ignore_for_now:
                property_value = config_dictionary[property_name]
                dict.__setitem__(config_obj, property_name, property_value)

            # Creates all components following the self-declared order attributes in
            # the factories.yaml file, registering each component as a property in this object
            for component_name in Config.__get_init_order(factories_dictionary):
                factory_def = factories_dictionary[component_name]['factory']

                factory_module = importlib.import_module(factory_def['module'])
                factory_class = getattr(factory_module, factory_def['class'])
                
                factory = factory_class(component_name)
                component_config = factory.config_handler.parse(
                    config_dictionary.get(component_name),
                    ConfigType(factories_dictionary[component_name]['type']),
                    context=config_obj
                )
                component = factory.create_component(component_config, context=config_obj)
                print(f"===> Component creation: {component_name} is {component}")
                
                # Sets the newly created component as a property in the config
                # object
                dict.__setitem__(config_obj, component_name, component)
            
            importlib.invalidate_caches()
            validator.check(config_obj)
        finally:
            dict.__delitem__(config_obj, 'tmp_vars')
        
    def __setattr__(self, attr,  value):
        # To the outside world, nothing can be set
        raise AttributeError("can't set attribute")


    