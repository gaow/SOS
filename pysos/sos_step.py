#!/usr/bin/env python3
#
# This file is part of Script of Scripts (sos), a workflow system
# for the execution of commands and scripts in different languages.
# Please visit https://github.com/bpeng2000/SOS
#
# Copyright (C) 2016 Bo Peng (bpeng@mdanderson.org)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
#
import os
import sys
import copy
import time
import glob
import fnmatch

from io import StringIO
from tokenize import generate_tokens
from collections.abc import Sequence, Iterable
from itertools import tee, combinations

from .utils import env, Error, AbortExecution, short_repr, get_traceback, pickleable, transcribe
from .pattern import extract_pattern
from .sos_eval import  SoS_eval, SoS_exec, Undetermined
from .target import BaseTarget, FileTarget, dynamic, RuntimeInfo
from .sos_syntax import SOS_INPUT_OPTIONS, SOS_DEPENDS_OPTIONS, SOS_OUTPUT_OPTIONS, \
    SOS_RUNTIME_OPTIONS

__all__ = []


class ExecuteError(Error):
    '''Raised when there are errors in prepare mode. Such errors are not raised
    immediately, but will be collected and raised at the end'''
    def __init__(self, workflow):
        Error.__init__(self, 'SoS workflow contains errors: %s' % workflow)
        self.workflow = workflow
        self.errors = []
        self.traces = []
        self.args = (workflow, )

    def append(self, line, error):
        lines = [x for x in line.split('\n') if x.strip()]
        if not lines:
            short_line = '<empty>'
        else:
            short_line = lines[0][:40] if len(lines[0]) > 40 else lines[0]
        self.errors.append(short_line)
        self.traces.append(get_traceback())
        if isinstance(error, Exception):
            self.message += '\n[%s] %s:\n\t%s' % (short_line, error.__class__.__name__, error)
        else:
            self.message += '\n[%s]:\n\t%s' % (short_line, error)


class StepInfo(object):
    '''A simple class to hold input, output, and index of step. Its attribute can
    only be set using an interface, and cannot be assigned. This is to make sure
    such information is not changed freely by malicious scripts'''
    def __init__(self):
        pass

    def set(self, key, value):
        object.__setattr__(self, key, value)

    def __setattr__(self, key, value):
        raise RuntimeError('Changing of step info {} is prohibited.'.format(key))

    def __repr__(self):
        return '{' + ', '.join('{}: {!r}'.format(x,y) for x,y in self.__dict__.items()) + '}'

def execute_task(task, global_def, sos_dict, sigil):
    '''A function that execute specified task within a local dictionary 
    (from SoS env.sos_dict). This function should be self-contained in that
    it can be handled by a task manager, be executed locally in a separate
    process or remotely on a different machine.'''
    env.register_process(os.getpid(), 'spawned_job with {} {}'
        .format(sos_dict['_input'], sos_dict['_output']))
    try:
        if '_runtime' in sos_dict and 'workdir' in sos_dict['_runtime']:
            os.chdir(os.path.expanduser(sos_dict['_runtime']['workdir']))
        env.sos_dict.quick_update(sos_dict)
        SoS_exec('import os, sys, glob')
        SoS_exec('from pysos.runtime import *')
        # re-execute global definition because some of the definitions in the 
        # global section might not be pickaleable (e.g. functions) and cannot
        # be passed to this separate process.
        if global_def:
            SoS_exec(global_def, sigil)
        # step process
        SoS_exec(task, sigil)
        os.chdir(env.exec_dir)
    except KeyboardInterrupt:
        raise RuntimeError('KeyboardInterrupt from {}'.format(os.getpid()))
    env.deregister_process(os.getpid())
    return {'succ': 0, 'output': env.sos_dict['_output']}

class Base_Step_Executor:
    # This base class defines how steps are executed. The derived classes will reimplement
    # some function to behave differently in different modes.
    #
    def __init__(self, step):
        self.step = step

    #
    # The following functions should be redefined in an executor
    # because it may behave differently in different modes.
    #
    def expand_input_files(self, value, *args):
        '''Process input files (perhaps a pattern) to determine input files.

        ret: 
            Return a file list or Undetermined.
        '''
        raise RuntimeError('Undefined virtual function.')

    def expand_depends_files(self, *args):
        '''Process dependent files (perhaps a pattern) to determine input files.

        ret: 
            Return a file list or Undetermined.
        '''
        raise RuntimeError('Undefined virtual function.')

    def expand_output_files(self, value, *args):
        '''Process output files (perhaps a pattern) to determine input files.
        '''
        if any(isinstance(x, dynamic) for x in args):
            return Undetermined(value)
        else:
            return _expand_file_list(True, *args)

    def verify_output(self):
        '''Check if intended output actually exists.'''
        pass

    def step_signature(self, index):
        '''Base executor does not have signature'''
        return None

    # Nested functions to handle different parameters of input directive
    @staticmethod
    def handle_group_by(ifiles, group_by):
        '''Handle input option group_by'''
        if group_by == 'single':
            return [[x] for x in ifiles]
        elif group_by == 'all':
            # default option
            return [ifiles]
        elif group_by == 'pairs':
            if len(ifiles) % 2 != 0:
                raise ValueError('Paired group_by has to have even number of input files: {} provided'
                    .format(len(ifiles)))
            return list(list(x) for x in zip(ifiles[:len(ifiles)//2], ifiles[len(ifiles)//2:]))
        elif group_by == 'pairwise':
            f1, f2 = tee(ifiles)
            next(f2, None)
            return [list(x) for x in zip(f1, f2)]
        elif group_by == 'combinations':
            return [list(x) for x in combinations(ifiles, 2)]
        elif isinstance(group_by, int) or group_by.isdigit():
            group_by = int(group_by)
            if len(ifiles) % group_by != 0:
                raise ValueError('Size of group_by block has to be divisible by the number of input files: {} provided'
                    .format(len(ifiles)))
            group_by = max(1, group_by)
            return [ifiles[i:i + group_by] for i in range(0, len(ifiles), group_by)]
        else:
            raise ValueError('Unsupported group_by option ``{}``!'.format(group_by))
    
    @staticmethod
    def handle_paired_with(paired_with, ifiles, _groups, _vars):
        '''Handle input option paired_with'''
        if paired_with is None or not paired_with:
            paired_with = []
        elif isinstance(paired_with, str):
            paired_with = [paired_with]
        elif isinstance(paired_with, Iterable):
            paired_with = paired_with
        else:
            raise ValueError('Unacceptable value for parameter paired_with: {}'.format(paired_with))
        #
        for wv in paired_with:
            if '.' in wv:
                if wv.split('.')[0] not in env.sos_dict:
                    raise ValueError('Variable {} does not exist.'.format(wv))
                values = getattr(env.sos_dict[wv.split('.')[0]], wv.split('.', 1)[-1])
            else:
                if wv not in env.sos_dict:
                    raise ValueError('Variable {} does not exist.'.format(wv))
                values = env.sos_dict[wv]
            if isinstance(values, str) or not isinstance(values, Iterable):
                raise ValueError('with_var variable {} is not a sequence ("{}")'.format(wv, values))
            if len(values) != len(ifiles):
                raise ValueError('Length of variable {} (length {}) should match the number of input files (length {}).'
                    .format(wv, len(values), len(ifiles)))
            file_map = {x:y for x,y in zip(ifiles, values)}
            for idx, grp in enumerate(_groups):
                _vars[idx]['_' + wv.split('.')[0]] = [file_map[x] for x in grp]

    @staticmethod
    def handle_extract_pattern(pattern, ifiles, _groups, _vars):
        '''Handle input option pattern'''
        if pattern is None or not pattern:
            patterns = []
        elif isinstance(pattern, str):
            patterns = [pattern]
        elif isinstance(pattern, Iterable):
            patterns = pattern
        else:
            raise ValueError('Unacceptable value for parameter pattern: {}'.format(pattern))
        #
        for pattern in patterns:
            res = extract_pattern(pattern, ifiles)
            # now, assign the variables to env
            for k, v in res.items():
                if k in ('input', 'output', 'depends') or k.startswith('_'):
                    raise RuntimeError('Pattern defined variable {} is not allowed'.format(k))
                env.sos_dict[k] = v
            # also make k, v pair with _input
            Base_Step_Executor.handle_paired_with(res.keys(), ifiles, _groups, _vars)

    @staticmethod
    def handle_for_each(for_each, _groups, _vars):
        if for_each is None or not for_each:
            for_each = []
        elif isinstance(for_each, str):
            for_each = [for_each]
        elif isinstance(for_each, Sequence):
            for_each = for_each
        else:
            raise ValueError('Unacceptable value for parameter for_each: {}'.format(for_each))
        #
        for fe_all in for_each:
            loop_size = None
            for fe in fe_all.split(','):
                values = env.sos_dict[fe]
                if not isinstance(values, Sequence):
                    try:
                        import pandas as pd
                        if not isinstance(values, pd.DataFrame):
                            raise ValueError('Unacceptable for_each data type {}'.format(values.__class__))
                    except Exception as e:
                        raise ValueError('Cannot iterate through variable {}: {}'.format(fe, e))
                if loop_size is None:
                    loop_size = len(values)
                elif loop_size != len(values):
                    raise ValueError('Length of variable {} (length {}) should match the length of variable {} (length {}).'
                        .format(fe, len(values), fe_all.split(',')[0], loop_size))
            # expand
            _tmp_groups = copy.deepcopy(_groups)
            _groups.clear()
            for i in range(loop_size):
                _groups.extend(_tmp_groups)
            #
            _tmp_vars = copy.deepcopy(_vars)
            _vars.clear()
            for vidx in range(loop_size):
                for idx in range(len(_tmp_vars)):
                    for fe in fe_all.split(','):
                        if '.' in fe:
                            if fe.split('.')[0] not in env.sos_dict:
                                raise ValueError('Variable {} does not exist.'.format(fe))
                            _tmp_vars[idx]['_' + fe.split('.')[0]] = getattr(env.sos_dict[fe.split('.')[0]], fe.split('.', 1)[-1])[vidx]
                        else:
                            if fe not in env.sos_dict:
                                raise ValueError('Variable {} does not exist.'.format(fe))
                            values = env.sos_dict[fe]
                            if isinstance(values, Sequence):
                                _tmp_vars[idx]['_' + fe] = values[vidx]
                            elif isinstance(values, pd.DataFrame):
                                _tmp_vars[idx]['_' + fe] = values.iloc[vidx]
                            else:
                                raise ValueError('Unrecognized for_each variable {}'.format(fe))
                _vars.extend(copy.deepcopy(_tmp_vars))

    # directive input
    def process_input_args(self, ifiles, **kwargs):
        '''This function handles directive input and all its parameters.
        It
            determines and set __step_input__
            determines and set pattern variables if needed
        returns
            _groups
            _vars
        which are groups of _input and related _vars
        '''
        if isinstance(ifiles, Undetermined):
            env.sos_dict.set('input', Undetermined())
            env.sos_dict.set('_input', Undetermined())
            # temporarily set depends and output to Undetermined because we cannot
            # go far with such input
            env.sos_dict.set('output', Undetermined())
            return [Undetermined()], [{}]

        for k in kwargs.keys():
            if k not in SOS_INPUT_OPTIONS:
                raise RuntimeError('Unrecognized input option {}'.format(k))
        #
        if 'filetype' in kwargs:
            if isinstance(kwargs['filetype'], str):
                ifiles = fnmatch.filter(ifiles, kwargs['filetype'])
            elif isinstance(kwargs['filetype'], Iterable):
                ifiles = [x for x in ifiles if any(fnmatch.fnmatch(x, y) for y in kwargs['filetype'])]
            elif callable(kwargs['filetype']):
                ifiles = [x for x in ifiles if kwargs['filetype'](x)]
        #
        # input file is the filtered files
        env.sos_dict.set('input', ifiles)
        env.sos_dict.set('_input', ifiles)
        #
        # handle group_by
        if 'group_by' in kwargs:
            _groups = Base_Step_Executor.handle_group_by(ifiles, kwargs['group_by'])
        else:
            _groups = [ifiles]
        #
        _vars = [{} for x in _groups]
        # handle paired_with
        if 'paired_with' in kwargs:
            Base_Step_Executor.handle_paired_with(kwargs['paired_with'], ifiles,  _groups, _vars)
        # handle pattern
        if 'pattern' in kwargs:
            Base_Step_Executor.handle_extract_pattern(kwargs['pattern'], ifiles, _groups, _vars)
        # handle for_each
        if 'for_each' in kwargs:
            Base_Step_Executor.handle_for_each(kwargs['for_each'], _groups, _vars)
        return _groups, _vars

    def process_depends_args(self, dfiles, **kwargs):
        for k in kwargs.keys():
            if k not in SOS_DEPENDS_OPTIONS:
                raise RuntimeError('Unrecognized depends option {}'.format(k))
        env.sos_dict.set('_depends', dfiles)
        if env.sos_dict['depends'] is None:
            env.sos_dict.set('depends', copy.deepcopy(dfiles))
        elif env.sos_dict['depends'] != dfiles:
            env.sos_dict['depends'].extend(dfiles)

    def process_output_args(self, ofiles, **kwargs):
        for k in kwargs.keys():
            if k not in SOS_OUTPUT_OPTIONS:
                raise RuntimeError('Unrecognized output option {}'.format(k))
        # create directory
        if not isinstance(ofiles, Undetermined):
            for ofile in ofiles:
                if isinstance(ofile, str):
                    parent_dir = os.path.split(os.path.expanduser(ofile))[0]
                    if parent_dir and not os.path.isdir(parent_dir):
                        os.makedirs(parent_dir)
        # set variables
        env.sos_dict.set('_output', ofiles)
        if isinstance(env.sos_dict['output'], (type(None), Undetermined)):
            env.sos_dict.set('output', copy.deepcopy(ofiles))
        elif not isinstance(env.sos_dict['output'], Undetermined) and env.sos_dict['output'] != ofiles:
            env.sos_dict['output'].extend(ofiles)

    def process_task_args(self, **kwargs):
        for k,v in kwargs.items():
            if k not in SOS_RUNTIME_OPTIONS:
                raise RuntimeError('Unrecognized runtime option {}={}'.format(k, v))
        env.sos_dict.set('_runtime', kwargs)

    def prepare_input_loop(self):
        # what do do before input loop
        pass

    def reevaluate_output(self):
        pass

    def submit_task(self):
        # submit results using single-thread
        # this is the default mode for prepare and interactive mode
        self.proc_results.append(
            execute_task(             # function
            self.step.task,           # task
            '',                       # local execusion, no need to re-run global
            # do not clone dict
            env.sos_dict,
            self.step.sigil
            ))

    def wait_for_results(self):
        # no waiting is necessary by default (prepare mode etc)
        pass

    def log(self, stage=None, msg=None):
        raise RuntimeError('Please redefine the log function in derived step executor.')

    def assign(self, key, value):
        try:
            env.sos_dict[key] = SoS_eval(value, self.step.sigil)
        except Exception as e:
            raise RuntimeError('Failed to assign {} to variable {}: {}'.format(value, key, e))

    def execute(self, stmt):
        try:
            self.last_res = SoS_exec(stmt, self.step.sigil)
        except AbortExecution:
            raise
        except Exception as e:
            raise RuntimeError('Failed to process statement {}: {}'.format(short_repr(stmt), e))

    def collect_result(self):
        # only results will be sent back to the master process
        #
        # __step_input__:    input of this step
        # __steo_output__:   output of this step
        # __step_depends__:  dependent files of this step
        result = {
            '__step_input__': env.sos_dict['input'],
            '__step_output__': env.sos_dict['output'],
            '__step_depends__': env.sos_dict['depends'],
            '__step_name__': env.sos_dict['step_name'],
        }
        if '__execute_errors__' in env.sos_dict:
            result['__execute_errors__'] = env.sos_dict['__execute_errors__']
        result['__changed_vars__'] = set()
        if 'alias' in self.step.options:
            step_info = StepInfo()
            step_info.set('step_name', env.sos_dict['step_name'])
            step_info.set('input', env.sos_dict['input'])
            step_info.set('output', env.sos_dict['output'])
            step_info.set('depends', env.sos_dict['depends'])
            # the step might be skipped
            for statement in self.step.statements:
                if statement[0] == '=' and statement[1] in env.sos_dict and pickleable(env.sos_dict[statement[1]]):
                    step_info.set(statement[1], env.sos_dict[statement[1]])
            if isinstance(self.step.options['alias'], Undetermined):
                # it is time to evalulate this expression now
                self.step.options['alias'] = self.step.options['alias'].value(self.step.sigil)
            result[self.step.options['alias']] = copy.deepcopy(step_info)
            result['__changed_vars__'].add(self.step.options['alias'])
        if 'shared' in self.step.options:
            try:
                vars = self.step.options['shared']
            except Exception as e:
                raise RuntimeError('Invalid option shared {}: {}'.format(self.step.options['shared'], e))
            if isinstance(vars, str):
                vars = [vars]
            elif not isinstance(vars, Sequence):
                raise ValueError('Option shared should be one or list of strings. {} provided'.format(vars))
            for var in vars:
                if var not in env.sos_dict:
                    raise RuntimeError('Variable {} is not defined to shared.'.format(var))
                result[var] = env.sos_dict[var]
            result['__changed_vars__'] |= set(vars)
        if hasattr(env, 'accessed_vars'):
            result['__environ_vars__'] = self.environ_vars
            result['__signature_vars__'] = env.accessed_vars
        return result

    def run(self):
        '''Execute a single step and return results. The result for batch mode is the
        input, output etc returned as alias, and for interactive mode is the return value
        of the last expression. '''
        # return value of the last executed statement
        self.last_res = None
        #
        self.log('start')
        # 
        # prepare environments, namely variables that can be used by the step
        #
        # * step_name:  name of the step, can be used by step process to determine 
        #               actions dynamically.
        if isinstance(self.step.index, int):
            env.sos_dict.set('step_name', '{}_{}'.format(self.step.name, self.step.index))
        else:
            env.sos_dict.set('step_name', '{}'.format(self.step.name))
        # used by nested workflow
        env.sos_dict.set('__step_context__', self.step.context)

        # * input:      input files, which should be __step_output__ if it is defined, or
        #               None otherwise. 
        # * _input:     first batch of input, which should be input if no input statement is used
        # * output:     None at first, can be redefined by output statement
        # * _output:    None at first, can be redefined by output statement
        # * depends:    None at first, can be redefined by depends statement
        # * _depends:   None at first, can be redefined by depends statement
        #
        if '__step_output__' not in env.sos_dict:
            env.sos_dict.set('input', None)
        else:
            if env.sos_dict['__step_output__'] is not None and not isinstance(env.sos_dict['__step_output__'], (list, Undetermined)):
                raise RuntimeError('__step_output__ can only be None, Undetermined, or a list of files.')
            env.sos_dict.set('input', copy.deepcopy(env.sos_dict['__step_output__']))

        # input can be Undetermined from undetermined output from last step
        env.sos_dict.set('_input', copy.deepcopy(env.sos_dict['input']))
        if '__default_output__' in env.sos_dict:
            env.sos_dict.set('output', copy.deepcopy(env.sos_dict['__default_output__']))
            env.sos_dict.set('_output', copy.deepcopy(env.sos_dict['__default_output__']))
        else:
            env.sos_dict.set('output', None)
            env.sos_dict.set('_output', None)
        env.sos_dict.set('depends', None)
        env.sos_dict.set('_depends', None)
        # _index is needed for pre-input action's active option and for debug output of scripts
        env.sos_dict.set('_index', 0)

        # look for input statement.
        input_statement_idx = [idx for idx,x in enumerate(self.step.statements) if x[0] == ':' and x[1] == 'input']
        if not input_statement_idx:
            input_statement_idx = None
        elif len(input_statement_idx) == 1:
            input_statement_idx = input_statement_idx[0]
        else:
            raise RuntimeError('More than one step input are specified in step {}_{}'.format(self.step.name, self.step.index))

        # if there is an input statement, execute the statements before it, and then the input statement
        if input_statement_idx is not None:
            # execute before input stuff
            for statement in self.step.statements[:input_statement_idx]:
                if statement[0] == '=':
                    self.assign(statement[1], statement[2])
                elif statement[0] == ':':
                    raise RuntimeError('Step input should be specified before others')
                else:
                    try:
                        self.execute(statement[1])
                    except AbortExecution as e:
                        if e.message:
                            env.logger.warning(e)
                        return self.collect_result()
            # input statement
            stmt = self.step.statements[input_statement_idx][2]
            self.log('input statement', stmt)
            try:
                args, kwargs = SoS_eval('__null_func__({})'.format(stmt), self.step.sigil)
                # Files will be expanded differently with different running modes
                input_files = self.expand_input_files(stmt, *args)
                self._groups, self._vars = self.process_input_args(input_files, **kwargs)
            except Exception as e:
                if '__execute_errors__' in env.sos_dict and env.sos_dict['__execute_errors__'].errors:
                    raise env.sos_dict['__execute_errors__']
                else:
                    raise RuntimeError('Failed to process input statement {}: {}'.format(stmt, e))

            input_statement_idx += 1
        else:
            # default case
            self._groups = [env.sos_dict['input']]
            self._vars = [{}]
            # assuming everything starts from 0 is after input
            input_statement_idx = 0

        self.log('input')
        
        self.proc_results = []
        # run steps after input statement, which will be run multiple times for each input 
        # group.
        env.sos_dict.set('__num_groups__', len(self._groups))

        self.prepare_input_loop()

        # determine if a single index or the whole step should be skipped
        skip_index = False
        # signatures of each index, which can remain to be None if no output
        # is defined.
        signatures = [None for x in self._groups]
        for idx, (g, v) in enumerate(zip(self._groups, self._vars)):
            # other variables
            env.sos_dict.update(v)
            env.sos_dict.set('_input', g)
            self.log('_input')
            env.sos_dict.set('_index', idx)
            for statement in self.step.statements[input_statement_idx:]:
                # if input is undertermined, we can only process output:
                if isinstance(g, Undetermined) and statement[0] != ':':
                    return self.collect_result()
                if statement[0] == '=':
                    self.assign(statement[1], statement[2])
                elif statement[0] == ':':
                    key, value, _ = statement[1:]
                    # output, depends, and process can be processed multiple times
                    try:
                        args, kwargs = SoS_eval('__null_func__({})'.format(value), self.step.sigil)
                        # dynamic output or dependent files
                        if key == 'output':
                            ofiles = self.expand_output_files(value, *args)
                            if not isinstance(g, (type(None), Undetermined)) and not isinstance(ofiles, (type(None), Undetermined)):
                                if any(x in g for x in ofiles):
                                    raise RuntimeError('Overlapping input and output files: {}'
                                        .format(', '.join(x for x in ofiles if x in g)))
                            # set variable _output and output
                            self.process_output_args(ofiles, **kwargs)

                            # ofiles can be Undetermined
                            sg = self.step_signature(idx)
                            if sg is not None and not isinstance(g, Undetermined):
                                signatures[idx] = RuntimeInfo(sg, env.sos_dict['_input'],
                                    env.sos_dict['_output'], env.sos_dict['_depends'])
                                if env.sig_mode == 'default':
                                    res = signatures[idx].validate()
                                    if res:
                                        # in this case, an Undetermined output can get real output files
                                        # from a signature
                                        ofiles = res['output']
                                        skip_index = True
                                elif env.sig_mode == 'assert':
                                    if not signatures[idx].validate():
                                        raise RuntimeError('Signature mismatch.')
                                elif env.sig_mode == 'construct':
                                    if signatures[idx].write():
                                        skip_index = True

                            if skip_index:
                                break
                        elif key == 'depends':
                            dfiles = self.expand_depends_files(*args)
                            # dfiles can be Undetermined
                            self.process_depends_args(dfiles, **kwargs)
                        elif key == 'task':
                            self.process_task_args(*args, **kwargs)
                        else:
                            raise RuntimeError('Unrecognized directive {}'.format(key))
                    except Exception as e:
                        # if input is Undertermined, it is possible that output cannot be processed
                        # due to that, and we just return
                        if isinstance(g, Undetermined):
                            return self.collect_result()
                        raise RuntimeError('Failed to process step {}: {} ({})'.format(key, value.strip(), e))
                else:
                    try:
                        self.execute(statement[1])
                    except AbortExecution as e:
                        if e.message:
                            env.logger.warning(e)
                        skip_index = True
                        break
            # if this index is skipped, go directly to the next one
            if skip_index:
                skip_index = False
                continue
            # finally, tasks..
            if not self.step.task:
                continue

            # check if the task is active
            if '_runtime' in env.sos_dict and 'active' in env.sos_dict['_runtime']:
                active = env.sos_dict['_runtime']['active']
                if isinstance(active, int):
                    if active >= 0 and env.sos_dict['_index'] != active:
                        continue
                    if active < 0 and env.sos_dict['_index'] != active + env.sos_dict['__num_groups__']:
                        continue
                elif isinstance(active, Sequence):
                    allowed_index = list([x if x >= 0 else env.sos_dict['__num_groups__'] + x for x in active])
                    if env.sos_dict['_index'] not in allowed_index:
                        continue
                elif isinstance(active, slice):
                    allowed_index = list(range(env.sos_dict['__num_groups__']))[active]
                    if env.sos_dict['_index'] not in allowed_index:
                        continue
                else:
                    raise RuntimeError('Unacceptable value for option active: {}'.format(active))

            self.log('task')
            try:
                self.submit_task()
            except Exception as e:
                # FIXME: cannot catch exception from subprocesses
                if env.verbosity > 2:
                    sys.stderr.write(get_traceback())
                raise RuntimeError('Failed to execute process\n"{}"\n{}'.format(short_repr(self.step.task), e))
            #
            # endfor loop for each input group
            #
        # check results? This is only meaningful for pool
        self.wait_for_results()
        # check results
        if not all(x['succ'] == 0 for x in self.proc_results):
            raise RuntimeError('Step process returns non-zero value')
        # if output is Undetermined, re-evalulate it
        #
        # NOTE: dynamic output is evaluated at last, so it sets output,
        # not _output. For the same reason, signatures can be wrong if it has
        # Undetermined output.
        if env.run_mode == 'run' and isinstance(env.sos_dict['output'], Undetermined):
            self.reevaluate_output()
            # if output is no longer Undetermined, set it to output
            # of each signature
            for sig in signatures:
                if sig is not None:
                    sig.set(env.sos_dict['output'], 'output')
        #
        for sig in signatures:
            if sig is not None:
                # signature write can fail for various reasons, for example when files are
                # not available in prepare mode.
                sig.write()
        self.log('output')
        self.verify_output()
        return self.collect_result()

class Queued_Step_Executor(Base_Step_Executor):
    # this class execute the step in a separate process
    # and returns result using a queue
    def __init__(self, step, queue):
        Base_Step_Executor.__init__(self, step)
        self.queue = queue

    def run(self):
        try:
            res = Base_Step_Executor.run(self)
            # shared variables will be sent from subprocess to the master
            for key in env.shared_vars:
                if key in env.sos_dict and key not in res:
                    res[key] = env.sos_dict[key]
            self.queue.put(res)
        except Exception as e:
            if env.verbosity > 2:
                sys.stderr.write(get_traceback())
            self.queue.put(e)

def _expand_file_list(ignore_unknown, *args):
    ifiles = []
    for arg in args:
        if arg is None:
            continue
        elif isinstance(arg, BaseTarget):
            ifiles.append(arg)
        elif isinstance(arg, str):
            ifiles.append(os.path.expanduser(arg))
        elif isinstance(arg, Iterable):
            # in case arg is a Generator, check its type will exhaust it
            arg = list(arg)
            if not all(isinstance(x, str) for x in arg):
                raise RuntimeError('Invalid file: {}'.format(arg))
            ifiles.extend(arg)
        else:
            raise RuntimeError('Unrecognized file: {}'.format(arg))

    # expand files with wildcard characters and check if files exist
    tmp = []
    for ifile in ifiles:
        if isinstance(ifile, BaseTarget):
            if ignore_unknown or ifile.exists():
                tmp.append(ifile)
            else:
                raise RuntimeError('{} not exist'.format(ifile))
        elif os.path.isfile(os.path.expanduser(ifile)):
            tmp.append(ifile)
        else:
            expanded = sorted(glob.glob(os.path.expanduser(ifile)))
            # no matching file ... but this is not a problem at the
            # inspection stage. 
            #
            # NOTE: if a DAG is given, the input file can be output from
            # a previous step..
            #
            if not expanded:
                if not ignore_unknown:
                    raise RuntimeError('{} not exist'.format(ifile))
                else:
                    tmp.append(ifile)
            else:
                tmp.extend(expanded)
    return tmp

class Prepare_Step_Executor(Queued_Step_Executor):
    def __init__(self, step, queue):
        env.run_mode = 'prepare'
        env.accessed_vars = set()
        Queued_Step_Executor.__init__(self, step, queue)

    def expand_input_files(self, value, *args):
        if any(isinstance(x, dynamic) for x in args):
            return Undetermined(value)
        # if unspecified, use __step_output__ as input (default)
        if not args:
            return env.sos_dict['input']
        else:
            return _expand_file_list(True, *args)

    def prepare_input_loop(self):
        # we start another round of check for accessed_vars so that we can create
        # signature for loops
        self.environ_vars = env.accessed_vars
        env.accessed_vars = set()

    def expand_depends_files(self, *args):
        '''handle directive depends'''
        if any(isinstance(x, dynamic) for x in args):
            return Undetermined()
        else:
            return _expand_file_list(True, *args)

    def log(self, stage=0, msg=None):
        if stage == 'start':
            env.logger.trace('Preparing ``{}_{}``: {}'.format(self.step.name, self.step.index, self.step.comment.strip()))

class SP_Step_Executor(Queued_Step_Executor):
    '''Single process step executor'''
    def __init__(self, step, queue):
        env.run_mode = 'run'
        if hasattr(env, 'accessed_vars'):
            delattr(env, 'accessed_vars')
        Queued_Step_Executor.__init__(self, step, queue)
        self.step_tokens = self.get_tokens()

    def get_tokens(self):
        '''Get tokens after input statement'''
        def _get_tokens(statement):
            return [x[1] for x in generate_tokens(StringIO(statement).readline)]

        tokens = []
        for statement in self.step.statements:
            if statement[0] == ':':
                # we only keep statements after input
                if statement[1] == 'input':
                    tokens = []
                continue
            if statement[0] == '=':
                tokens.extend([statement[1], statement[0]])
                tokens.extend(_get_tokens(statement[2]))
            else:
                tokens.extend(_get_tokens(statement[1]))

        if self.step.task:
            tokens.extend(_get_tokens(self.step.task))

        return ''.join(tokens)

    def log(self, stage=None, msg=None):
        if stage == 'start':
            if isinstance(self.step.index, int):
                env.logger.info('Executing ``{}_{}``: {}'.format(self.step.name, self.step.index, self.step.comment.strip()))
            else:
                env.logger.info('Executing ``{}``: {}'.format(self.step.name, self.step.comment.strip()))
        elif stage == 'input statement':
            env.logger.trace('Handling input statement {}'.format(msg))
        elif stage == '_input':
            if env.sos_dict['_input'] is not None:
                env.logger.debug('_input: ``{}``'.format(short_repr(env.sos_dict['_input'])))
        elif stage == 'input':
            if env.sos_dict['input'] is not None:
                env.logger.info('input:    ``{}``'.format(short_repr(env.sos_dict['input'])))
        elif stage == 'output':
            if env.sos_dict['output'] is not None:
                env.logger.info('output:   ``{}``'.format(short_repr(env.sos_dict['output'])))

    def assign(self, key, value):
        Base_Step_Executor.assign(self, key, value)
        transcribe('{} = {}'.format(key, env.sos_dict[key]))

    def expand_input_files(self, value, *args):
        # if unspecified, use __step_output__ as input (default)
        # resolve dynamic input.
        args = [x.resolve() if isinstance(x, dynamic) else x for x in args]
        if not args:
            return env.sos_dict['input']
        else:
            return _expand_file_list(False, *args)

    def step_signature(self, index):
        '''returns a signature of the step. Change of the step content will
        lead to the invalidation of the signature, which will then cause the
        re-execution of the step for any result from the step. '''
        #
        if env.sig_mode == 'ignore':
            return None
        env_vars = []
        for var in sorted(env.sos_dict['__signature_vars__']):
            # var can be local and not passed as outside environment
            if var in env.sos_dict:
                env_vars.append('{} = {}\n'.format(var, env.sos_dict[var]))

        return ''.join(env_vars) + '---\n' + self.step_tokens

    def expand_depends_files(self, *args, **kwargs):
        '''handle directive depends'''
        return _expand_file_list(False, *args)

    def reevaluate_output(self):
        # re-process the output statement to determine output files
        args, kwargs = SoS_eval('__null_func__({})'.format(env.sos_dict['output'].expr), self.step.sigil)
        # handle dynamic args
        args = [x.resolve() if isinstance(x, dynamic) else x for x in args]
        env.sos_dict.set('output', self.expand_output_files('', *args))

    def verify_output(self):
        if env.sos_dict['output'] is None:
            return
        if isinstance(env.sos_dict['output'], Undetermined):
            raise RuntimeError('Output of a completed step cannot be undetermined.')
        for target in env.sos_dict['output']:
            if isinstance(target, str):
                if not FileTarget(target).exists('any'):
                    raise RuntimeError('Output target {} does not exist after the completion of step {}'
                            .format(target, env.sos_dict['step_name']))
            elif not target.exists('any'):
                raise RuntimeError('Output target {} does not exist after the completion of step {}'
                            .format(target, env.sos_dict['step_name']))
        

class MP_Step_Executor(SP_Step_Executor):
    def __init__(self, step, queue):
        SP_Step_Executor.__init__(self, step, queue)
        self.pool = None

    def submit_task(self):
        # if concurrent is set, create a pool object
        import multiprocessing as mp
        if self.pool is None and env.max_jobs > 1 and len(self._groups) > 1 and \
            'concurrent' in env.sos_dict['_runtime'] and env.sos_dict['_runtime']['concurrent']:
            self.pool = mp.Pool(min(env.max_jobs, len(self._groups)))

        if self.pool:
            self.proc_results.append(self.pool.apply_async(
                execute_task,            # function
                (self.step.task,         # task
                self.step.global_def,    # global process
                # if pool, it must not be in prepare mode and have
                # __signature_vars__
                env.sos_dict.clone_selected_vars(env.sos_dict['__signature_vars__'] \
                    | {'_input', '_output', '_depends', 'input', 'output', 'depends', '_idx'}),
                self.step.sigil
                )))
        else:
            # execute in existing process
            self.proc_results.append(
                execute_task(             # function
                self.step.task,           # task
                '',                       # local execusion, no need to re-run global
                # do not clone dict
                env.sos_dict,
                self.step.sigil
                ))

    def wait_for_results(self):
        from multiprocessing.pool import AsyncResult
        if self.pool is None:
            return
        try:
            self.proc_results = [res.get() if isinstance(res, AsyncResult) else res for res in self.proc_results]
        except KeyboardInterrupt:
            # if keyboard interrupt
            raise RuntimeError('KeyboardInterrupt fro m {} (master)'.format(os.getpid()))
        except Exception as e:
            # if keyboard interrupt etc
            env.logger.error('Caught {}'.format(e))
            raise
        finally:
            # finally, write results back to the master process
            self.pool.terminate()
            self.pool.close()
            self.pool.join()


class RQ_Step_Executor(SP_Step_Executor):
    #
    # This is not working yet
    def __init__(self, step, queue, redis_queue):
        SP_Step_Executor.__init__(self, step, queue)
        self.redis_queue = redis_queue

    def submit_task(self):
        self.proc_results.append( self.redis_queue.enqueue(
            execute_task,            # function
            self.step.task,          # task
            self.step.global_def,    # global process
            # if pool, it must not be in prepare mode and have
            # __signature_vars__
            env.sos_dict.clone_selected_vars(env.sos_dict['__signature_vars__'] \
                | {'_input', '_output', '_depends', 'input', 'output', 'depends', '_idx'}),
            self.step.sigil
            ) )

    def wait_for_results(self):
        while True:
            # wait for results
            try:
                if any(x.result is None for x in self.proc_results):
                    time.sleep(1)
                else:
                    # the executor interface expect results ...
                    self.proc_results = [x.result for x in self.proc_results]
                    return
            except KeyboardInterrupt:
                # if keyboard interrupt
                raise RuntimeError('KeyboardInterrupt fro m {} (master)'.format(os.getpid()))
            except Exception as e:
                # if keyboard interrupt etc
                env.logger.error('Caught {}'.format(e))
                raise

class Celery_Step_Executor(SP_Step_Executor):
    def __init__(self, step, queue):
        SP_Step_Executor.__init__(self, step, queue)

        # make execute_task a celery task ... 
        from celery import shared_task
        execute_task.celery = shared_task(execute_task)

    def submit_task(self):
        # if concurrent is set, create a pool object
        self.proc_results.append( 
            execute_task.celery.delay(     # function
            self.step.task,         # task
            self.step.global_def,   # global process
            env.sos_dict.clone_selected_vars(env.sos_dict['__signature_vars__'] \
                | {'_input', '_output', '_depends', 'input', 'output', 'depends', '_idx'}),
            self.step.sigil
            ) )

    def wait_for_results(self):
        while True:
            # wait for results
            try:
                if any(not x.ready() for x in self.proc_results):
                    time.sleep(1)
                else:
                    break
                env.logger.error('{}/{} completed'.format(len([x for x in self.proc_results if x.ready()]), len(self.proc_results)))
            except KeyboardInterrupt:
                # if keyboard interrupt
                raise RuntimeError('KeyboardInterrupt fro m {} (master)'.format(os.getpid()))
            except Exception as e:
                # if keyboard interrupt etc
                env.logger.error('Caught {}'.format(e))
                raise

class Interactive_Step_Executor(Base_Step_Executor):
    def __init__(self, step):
        env.run_mode = 'interactive'
        Base_Step_Executor.__init__(self, step)
    
    def expand_input_files(self, value, *args):
        # We ignore 'dynamic' option in run mode
        # if unspecified, use __step_output__ as input (default)
        if not args:
            return env.sos_dict['input']
        else:
            return _expand_file_list(False, *args)

    def expand_depends_files(self, *args):
        '''handle directive depends'''
        return _expand_file_list(False, *args)

    def expand_output_files(self, value, *args):
        return _expand_file_list(True, *args)

    def log(self, stage=None, msg=None):
        return

    def collect_result(self):
        return self.last_res
