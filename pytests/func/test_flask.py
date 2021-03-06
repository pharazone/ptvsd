# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See LICENSE in the project root
# for license information.

from __future__ import print_function, with_statement, absolute_import

import os.path
import platform
import pytest
import sys

from pytests.helpers.pattern import ANY
from pytests.helpers.session import DebugSession
from pytests.helpers.timeline import Event
from pytests.helpers.webhelper import get_web_content, wait_for_connection
from pytests.helpers.pathutils import get_test_root, compare_path
from pytests.helpers.session import START_METHOD_LAUNCH, START_METHOD_CMDLINE


FLASK1_ROOT = get_test_root('flask1')
FLASK1_APP = os.path.join(FLASK1_ROOT, 'app.py')
FLASK1_TEMPLATE = os.path.join(FLASK1_ROOT, 'templates', 'hello.html')
FLASK_LINK = 'http://127.0.0.1:5000/'
FLASK_PORT = 5000

def _flask_no_multiproc_common(debug_session, start_method):
    env = {
        'FLASK_APP': 'app.py',
        'FLASK_ENV': 'development',
        'FLASK_DEBUG': '0',
    }
    if platform.system() != 'Windows':
        locale = 'en_US.utf8' if platform.system() == 'Linux' else 'en_US.UTF-8'
        env.update({
            'LC_ALL': locale,
            'LANG': locale,
        })

    debug_session.initialize(
        start_method=start_method,
        target=('module', 'flask'),
        program_args=['run', '--no-debugger', '--no-reload', '--with-threads'],
        ignore_events=[Event('stopped'), Event('continued')],
        debug_options=['Jinja'],
        cwd=FLASK1_ROOT,
        env=env,
        expected_returncode=ANY.int,  # No clean way to kill Flask server
    )


@pytest.mark.parametrize('bp_file, bp_line, bp_name', [
  (FLASK1_APP, 11, 'home'),
  (FLASK1_TEMPLATE, 8, 'template'),
])
@pytest.mark.parametrize('start_method', [START_METHOD_LAUNCH, START_METHOD_CMDLINE])
@pytest.mark.timeout(60)
def test_flask_breakpoint_no_multiproc(debug_session, bp_file, bp_line, bp_name, start_method):
    _flask_no_multiproc_common(debug_session, start_method)

    bp_var_content = 'Flask-Jinja-Test'
    debug_session.set_breakpoints(bp_file, [bp_line])
    debug_session.start_debugging()

    # wait for Flask web server to start
    wait_for_connection(FLASK_PORT)
    link = FLASK_LINK
    web_request = get_web_content(link, {})

    thread_stopped = debug_session.wait_for_next(Event('stopped'), ANY.dict_with({'reason': 'breakpoint'}))
    assert thread_stopped.body['threadId'] is not None

    tid = thread_stopped.body['threadId']

    resp_stacktrace = debug_session.send_request('stackTrace', arguments={
        'threadId': tid,
    }).wait_for_response()
    assert resp_stacktrace.body['totalFrames'] > 0
    frames = resp_stacktrace.body['stackFrames']
    assert frames[0] == {
        'id': ANY.int,
        'name': bp_name,
        'source': {
            'sourceReference': ANY.int,
            'path': ANY.such_that(lambda s: compare_path(s, bp_file)),
        },
        'line': bp_line,
        'column': 1,
    }

    fid = frames[0]['id']
    resp_scopes = debug_session.send_request('scopes', arguments={
        'frameId': fid
    }).wait_for_response()
    scopes = resp_scopes.body['scopes']
    assert len(scopes) > 0

    resp_variables = debug_session.send_request('variables', arguments={
        'variablesReference': scopes[0]['variablesReference']
    }).wait_for_response()
    variables = list(v for v in resp_variables.body['variables'] if v['name'] == 'content')
    assert variables == [{
            'name': 'content',
            'type': 'str',
            'value': repr(bp_var_content),
            'presentationHint': {'attributes': ['rawString']},
            'evaluateName': 'content'
        }]

    debug_session.send_request('continue').wait_for_response()
    debug_session.wait_for_next(Event('continued'))

    web_content = web_request.wait_for_response()
    assert web_content.find(bp_var_content) != -1

    # shutdown to web server
    link = FLASK_LINK + 'exit'
    get_web_content(link).wait_for_response()

    debug_session.wait_for_exit()


@pytest.mark.parametrize('ex_type, ex_line', [
  ('handled', 21),
  ('unhandled', 33),
])
@pytest.mark.parametrize('start_method', [START_METHOD_LAUNCH, START_METHOD_CMDLINE])
@pytest.mark.timeout(60)
def test_flask_exception_no_multiproc(debug_session, ex_type, ex_line, start_method):
    _flask_no_multiproc_common(debug_session, start_method)

    debug_session.send_request('setExceptionBreakpoints', arguments={
        'filters': ['raised', 'uncaught'],
    }).wait_for_response()

    debug_session.start_debugging()

    # wait for Flask web server to start
    wait_for_connection(FLASK_PORT)
    base_link = FLASK_LINK
    link = base_link + ex_type if base_link.endswith('/') else ('/' + ex_type)
    web_request = get_web_content(link, {})

    thread_stopped = debug_session.wait_for_next(Event('stopped', ANY.dict_with({'reason': 'exception'})))
    assert thread_stopped == Event('stopped', ANY.dict_with({
        'reason': 'exception',
        'text': ANY.such_that(lambda s: s.endswith('ArithmeticError')),
        'description': 'Hello'
    }))

    tid = thread_stopped.body['threadId']
    resp_exception_info = debug_session.send_request(
        'exceptionInfo',
        arguments={'threadId': tid, }
    ).wait_for_response()
    exception = resp_exception_info.body
    assert exception == {
        'exceptionId': ANY.such_that(lambda s: s.endswith('ArithmeticError')),
        'breakMode': 'always',
        'description': 'Hello',
        'details': {
            'message': 'Hello',
            'typeName': ANY.such_that(lambda s: s.endswith('ArithmeticError')),
            'source': ANY.such_that(lambda s: compare_path(s, FLASK1_APP)),
            'stackTrace': ANY.such_that(lambda s: True)
        }
    }

    resp_stacktrace = debug_session.send_request('stackTrace', arguments={
        'threadId': tid,
    }).wait_for_response()
    assert resp_stacktrace.body['totalFrames'] > 0
    frames = resp_stacktrace.body['stackFrames']
    assert frames[0] == {
        'id': ANY.int,
        'name': 'bad_route_' + ex_type,
        'source': {
            'sourceReference': ANY.int,
            'path': ANY.such_that(lambda s: compare_path(s, FLASK1_APP)),
        },
        'line': ex_line,
        'column': 1,
    }

    debug_session.send_request('continue').wait_for_response()
    debug_session.wait_for_next(Event('continued'))

    # ignore response for exception tests
    web_request.wait_for_response()

    # shutdown to web server
    link = base_link + 'exit' if base_link.endswith('/') else '/exit'
    get_web_content(link).wait_for_response()

    debug_session.wait_for_exit()

def _wait_for_child_process(debug_session):
    child_subprocess = debug_session.wait_for_next(Event('ptvsd_subprocess'))
    assert child_subprocess.body['port'] != 0

    child_port = child_subprocess.body['port']

    child_session = DebugSession(start_method=START_METHOD_CMDLINE, ptvsd_port=child_port)
    child_session.ignore_unobserved = debug_session.ignore_unobserved
    child_session.debug_options = debug_session.debug_options
    child_session.connect()
    child_session.handshake()
    return child_session


@pytest.mark.timeout(120)
@pytest.mark.parametrize('start_method', [START_METHOD_LAUNCH])
@pytest.mark.skipif((sys.version_info < (3, 0)) and (platform.system() != 'Windows'), reason='Bug #935')
def test_flask_breakpoint_multiproc(debug_session, start_method):
    env = {
        'FLASK_APP': 'app',
        'FLASK_ENV': 'development',
        'FLASK_DEBUG': '1',
    }
    if platform.system() != 'Windows':
        locale = 'en_US.utf8' if platform.system() == 'Linux' else 'en_US.UTF-8'
        env.update({
            'LC_ALL': locale,
            'LANG': locale,
        })

    debug_session.initialize(
        start_method=start_method,
        target=('module', 'flask'),
        multiprocess=True,
        program_args=['run', ],
        ignore_events=[Event('stopped'), Event('continued')],
        debug_options=['Jinja'],
        cwd=FLASK1_ROOT,
        env=env,
        expected_returncode=ANY.int,  # No clean way to kill Flask server
    )

    bp_line = 11
    bp_var_content = 'Flask-Jinja-Test'
    debug_session.set_breakpoints(FLASK1_APP, [bp_line])
    debug_session.start_debugging()
    child_session = _wait_for_child_process(debug_session)

    child_session.send_request('setBreakpoints', arguments={
        'source': {'path': FLASK1_APP},
        'breakpoints': [{'line': bp_line}, ],
    }).wait_for_response()
    child_session.start_debugging()

    # wait for Flask server to start
    wait_for_connection(FLASK_PORT)
    web_request = get_web_content(FLASK_LINK, {})

    thread_stopped = child_session.wait_for_next(Event('stopped', ANY.dict_with({'reason': 'breakpoint'})))
    assert thread_stopped.body['threadId'] is not None

    tid = thread_stopped.body['threadId']

    resp_stacktrace = child_session.send_request('stackTrace', arguments={
        'threadId': tid,
    }).wait_for_response()
    assert resp_stacktrace.body['totalFrames'] > 0
    frames = resp_stacktrace.body['stackFrames']
    assert frames[0] == {
        'id': ANY.int,
        'name': 'home',
        'source': {
            'sourceReference': ANY.int,
            'path': ANY.such_that(lambda s: compare_path(s, FLASK1_APP)),
        },
        'line': bp_line,
        'column': 1,
    }

    fid = frames[0]['id']
    resp_scopes = child_session.send_request('scopes', arguments={
        'frameId': fid
    }).wait_for_response()
    scopes = resp_scopes.body['scopes']
    assert len(scopes) > 0

    resp_variables = child_session.send_request('variables', arguments={
        'variablesReference': scopes[0]['variablesReference']
    }).wait_for_response()
    variables = [v for v in resp_variables.body['variables'] if v['name'] == 'content']
    assert variables == [{
            'name': 'content',
            'type': 'str',
            'value': repr(bp_var_content),
            'presentationHint': {'attributes': ['rawString']},
            'evaluateName': 'content'
        }]

    child_session.send_request('continue').wait_for_response()
    child_session.wait_for_next(Event('continued'))

    web_content = web_request.wait_for_response()
    assert web_content.find(bp_var_content) != -1

    # shutdown to web server
    link = FLASK_LINK + 'exit'
    get_web_content(link).wait_for_response()

    debug_session.wait_for_exit()
