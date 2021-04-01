'''
This plugin minimizes Pylint's complaints about web2py code.
Web2py executes user code in special environment populated with predefined objects and types and with objects defined in model files.
Also it has magic import mechanism which knows some special places where to find modules.

Pylint doesn't know about these details -- its parser is unable to find these objects and modules, resulting in a flood of laments.
This plugin:
- adds variables defined in models to other models' and controllers' scope
- adds definition of some predefined global objects to models and controllers
- adds web2py module paths to PYTHONPATH
'''
from astroid import MANAGER, scoped_nodes
from astroid.builder import AstroidBuilder
from pylint.lint import PyLinter
from pylint.checkers.variables import VariablesChecker
from pylint.interfaces import UNDEFINED
from pylint.utils import PyLintASTWalker
from os.path import join, splitext
import os
import re
import sys
import ipdb

if os.sep == '\\':

    web2py_pattern = r'(.+?)\\applications\\(.+?)\\(.+?)\\'
else:
    web2py_pattern = r'(.+?)/applications/(.+?)/(.+?)/' 

def register(_):
    'Register web2py transformer, called by pylint'
    MANAGER.register_transform(scoped_nodes.Module, web2py_transform)

def web2py_transform(module):
    'Add imports and some default objects, add custom module paths to pythonpath'

    if module.file:
        #Check if this file belongs to web2py
        web2py_match = re.match(web2py_pattern, module.file)
        if web2py_match:
            web2py_path, app_name, subfolder = web2py_match.group(1), web2py_match.group(2), web2py_match.group(3)
            return transformer.transform_module(module, web2py_path, app_name, subfolder)

class Web2PyTransformer(object):
    'Transforms web2py modules code'
    # This dummy code is copied from gluon/__init__.py
    fake_code = '''
from gluon.html import *
from gluon.validators import *
from gluon.http import redirect, HTTP
from gluon.dal import DAL, Field
from gluon.sqlhtml import SQLFORM, SQLTABLE
from gluon.compileapp import LOAD

from gluon.globals import Request, Response, Session
from gluon.cache import Cache
from gluon.languages import translator
from gluon.tools import Auth, Crud, Mail, Service, PluginManager

# API objects
request = Request()
response = Response()
session = Session()
cache = Cache(request)
T = translator(request)
'''

    def __init__(self):
        '''
        self.top_level: are we dealing with the original passed file?
        Pylint will recursively parse imports and models, we don't want to transform them
        '''
        self.is_pythonpath_modified = False
        self.app_model_names = []
        self.top_level = True

    def transform_module(self, module_node, web2py_path, app_name, subfolder):
        'Determine the file type (model, controller or module) and transform it'
        if not self.top_level:
            return module_node

        #Add web2py modules paths to sys.path
        self._add_paths(web2py_path, app_name)

        if subfolder == 'models':
            self.top_level = False
            transformed_module = self._trasform_model(module_node)
            self.top_level = True
        elif subfolder == 'controllers':
            self.top_level = False
            transformed_module = self._transform_controller(module_node)
            self.top_level = True
        else:
            transformed_module = module_node

        return transformed_module

    def _add_paths(self, web2py_path, app_name):
        '''
Add web2py module paths to sys.path
Add models path too to be able to import it from the fake code
        '''
        if not self.is_pythonpath_modified:
            gluon_path = join(web2py_path, 'gluon')
            site_packages_path = join(web2py_path, 'site-packages')
            app_modules_path = join(web2py_path, 'applications', app_name, 'modules')
            app_models_path = join(web2py_path, 'applications', app_name, 'models') #Add models to import them them in controllers

            for module_path in [gluon_path, site_packages_path, app_modules_path, app_models_path, web2py_path]:
                sys.path.append(module_path)

            self._fill_app_model_names(app_models_path)

            self.is_pythonpath_modified = True


    def _trasform_model(self, module_node):
        'Add globals from fake code + import code from previous (in alphabetical order) models'
        fake_code = self.fake_code + self._gen_models_import_code(module_node.name)
        fake = AstroidBuilder(MANAGER).string_build(fake_code)
        module_node.locals.update(fake.globals)

        module_node = self._remove_unused_imports(module_node, fake)
        return module_node

    def _transform_controller(self, module_node):
        'Add globals from fake code + import models'
        fake_code = self.fake_code + self._gen_models_import_code()

        fake = AstroidBuilder(MANAGER).string_build(fake_code)
        module_node.locals.update(fake.globals)

        module_node = self._remove_unused_imports(module_node, fake)

        return module_node

    def _gen_models_import_code(self, current_model=None):
        'Generate import code for models (only previous in alphabetical order if called by model)'
        code = ''
        for model_name in self.app_model_names:
            if current_model and model_name == current_model:
                break
            code += 'from %s import *\n' % model_name

        return code

    def _fill_app_model_names(self, app_models_path):
        'Save model names for later use'
        model_files = os.listdir(app_models_path)
        model_files = [model_file for model_file in model_files if re.match(r'.+?\.py$', model_file)] #Only top-level models
        model_files = sorted(model_files) #Models are executed in alphabetical order
        self.app_model_names = [re.match(r'^(.+?)\.py$', model_file).group(1) for model_file in model_files]

    def _remove_unused_imports(self, module_node, fake_node):
        '''
We import objects from fake code and from models, so pylint doesn't complain about undefined objects.
But now it complains a lot about unused imports.
We cannot suppress it, so we call VariableChecker with fake linter to intercept and collect all such error messages,
and then use them to remove unused imports.
        '''
        #Needed for removal of unused import messages
        sniffer = MessageSniffer() #Our linter substitution
        walker = PyLintASTWalker(sniffer)
        var_checker = VariablesChecker(sniffer)
        walker.add_checker(var_checker)

        #Collect unused import messages
        sniffer.set_fake_node(fake_node)
        sniffer.check_astroid_module(module_node, walker, [], [])

        #Remove unneeded globals imported from fake code
        for name in sniffer.unused:
            if name in fake_node.globals and \
              name in module_node.locals: #Maybe it's already deleted
                del module_node.locals[name]

        return module_node

class MessageSniffer(PyLinter):
    'Special class to mimic PyLinter to intercept messages from checkers. Here we use it to collect info about unused imports'
    def __init__(self):
        super(MessageSniffer, self).__init__()
        self.unused = set()
        self.walker = None
        self.fake_node = None

    def set_fake_node(self, fake_node):
        'We need fake node to distinguish real unused imports in user code from unused imports induced by our fake code'
        self.fake_node = fake_node
        self.unused = set()

    def add_message(self, msg_descr, line=None, node=None, args=None, confidence=UNDEFINED):
        'Message interceptor'
        if msg_descr == 'unused-wildcard-import':
            self.unused.add(args)

        elif msg_descr == 'unused-import':
            #Unused module or unused symbol from module, extract with regex
            sym_match = re.match(r'^(.+?)\ imported\ from', args)
            if sym_match:
                sym_name = sym_match.group(1)
            else:
                module_match = re.match(r'^import\ (.+?)$', args)
                assert module_match
                sym_name = module_match.group(1)

            if sym_name in self.fake_node.globals:
                self.unused.add(sym_name)

transformer = Web2PyTransformer()
