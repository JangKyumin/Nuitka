#     Copyright 2018, Kay Hayen, mailto:kay.hayen@gmail.com
#
#     Part of "Nuitka", an optimizing Python compiler that is compatible and
#     integrates with CPython, but also works on its own.
#
#     Licensed under the Apache License, Version 2.0 (the "License");
#     you may not use this file except in compliance with the License.
#     You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#     Unless required by applicable law or agreed to in writing, software
#     distributed under the License is distributed on an "AS IS" BASIS,
#     WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#     See the License for the specific language governing permissions and
#     limitations under the License.
#
""" Code to generate and interact with compiled coroutine objects.

"""

from .CodeHelpers import (
    decideConversionCheckNeeded,
    generateChildExpressionsCode,
    generateStatementSequenceCode
)
from .Emission import SourceCodeCollector
from .ErrorCodes import getErrorExitCode
from .FunctionCodes import (
    finalizeFunctionLocalVariables,
    getClosureCopyCode,
    getFunctionQualnameObj,
    setupFunctionLocalVariables
)
from .Indentation import indented
from .LineNumberCodes import emitLineNumberUpdateCode
from .ModuleCodes import getModuleAccessCode
from .templates.CodeTemplatesCoroutines import (
    template_coroutine_exception_exit,
    template_coroutine_noexception_exit,
    template_coroutine_object_body_template,
    template_coroutine_object_maker_template,
    template_coroutine_return_exit,
    template_make_coroutine
)
from .YieldCodes import _getYieldPreserveCode, getYieldReturnDispatchCode


def getCoroutineObjectDeclCode(function_identifier):
    return template_coroutine_object_maker_template % {
        "function_identifier" : function_identifier,
    }


def getCoroutineObjectCode(context, function_identifier, closure_variables,
                           user_variables, outline_variables,
                           temp_variables, needs_exception_exit,
                           needs_generator_return):
    # A bit of details going on here, pylint: disable=too-many-locals

    setupFunctionLocalVariables(
        context           = context,
        parameters        = None,
        closure_variables = closure_variables,
        user_variables    = user_variables + outline_variables,
        temp_variables    = temp_variables
    )

    function_codes = SourceCodeCollector()

    coroutine_object_body = context.getOwner()

    generateStatementSequenceCode(
        statement_sequence = coroutine_object_body.getBody(),
        allow_none         = True,
        emit               = function_codes,
        context            = context
    )

    function_cleanup = finalizeFunctionLocalVariables(context)

    if needs_exception_exit:
        exception_type, exception_value, exception_tb, _exception_lineno = \
          context.variable_storage.getExceptionVariableDescriptions()

        generator_exit = template_coroutine_exception_exit % {
            "function_identifier" : function_identifier,
            "function_cleanup"    : indented(function_cleanup),
            "exception_type"      : exception_type,
            "exception_value"     : exception_value,
            "exception_tb"        : exception_tb
        }
    else:
        generator_exit = template_coroutine_noexception_exit % {
            "function_identifier" : function_identifier,
            "function_cleanup"    : indented(function_cleanup)
        }

    if needs_generator_return:
        generator_exit += template_coroutine_return_exit % {
            "return_value" : context.getReturnValueName()
        }

    function_locals = context.variable_storage.makeCFunctionLevelDeclarations()

    local_type_decl = context.variable_storage.makeCStructLevelDeclarations()
    function_locals += context.variable_storage.makeCStructInits()

    if local_type_decl:
        heap_declaration = """\
struct %(function_identifier)s_locals *coroutine_heap = \
(struct %(function_identifier)s_locals *)coroutine->m_heap_storage;""" % {
            "function_identifier" : function_identifier
        }
    else:
        heap_declaration = ""

    return template_coroutine_object_body_template % {
        "function_identifier"    : function_identifier,
        "function_body"          : indented(function_codes.codes),
        "heap_declaration"       : indented(heap_declaration),
        "function_local_types"   : indented(local_type_decl),
        "function_var_inits"     : indented(function_locals),
        "function_dispatch"      : indented(getYieldReturnDispatchCode(context)),
        "coroutine_exit"         : generator_exit,
        "coroutine_module"       : getModuleAccessCode(context),
        "coroutine_name_obj"     : context.getConstantCode(
            constant = coroutine_object_body.getFunctionName()
        ),
        "coroutine_qualname_obj" : getFunctionQualnameObj(coroutine_object_body, context),
        "code_identifier"        : context.getCodeObjectHandle(
            code_object = coroutine_object_body.getCodeObject(),
        ),
        "closure_count"          : len(closure_variables)
    }


def generateMakeCoroutineObjectCode(to_name, expression, emit, context):
    coroutine_object_body = expression.getCoroutineRef().getFunctionBody()

    closure_variables = expression.getClosureVariableVersions()

    closure_copy = getClosureCopyCode(
        to_name           = to_name,
        closure_type      = "struct Nuitka_CoroutineObject *",
        closure_variables = closure_variables,
        context           = context
    )

    emit(
        template_make_coroutine % {
            "to_name"              : to_name,
            "coroutine_identifier" : coroutine_object_body.getCodeName(),
            "closure_copy"         : indented(closure_copy, 0, True),
        }
    )

    context.addCleanupTempName(to_name)


def generateAsyncWaitCode(to_name, expression, emit, context):
    emitLineNumberUpdateCode(emit, context)

    value_name, = generateChildExpressionsCode(
        expression = expression,
        emit       = emit,
        context    = context
    )

    # In handlers, we must preserve/restore the exception.
    preserve_exception = expression.isExceptionPreserving()

    if expression.isExpressionAsyncWaitEnter():
        wait_kind = "await_enter"
    elif expression.isExpressionAsyncWaitExit():
        wait_kind = "await_exit"
    else:
        wait_kind = "await_normal"

    # In handlers, we must preserve/restore the exception.

    iter_name = context.allocateTempName("async_wait")

    emit(
        "%s = ASYNC_AWAIT( %s, %s );" % (
            iter_name,
            value_name,
            wait_kind
        )
    )

    getErrorExitCode(
        check_name   = iter_name,
        release_name = value_name,
        emit         = emit,
        context      = context
    )

    assert not context.needsCleanup(iter_name)

    yield_code = """\
%(object_name)s->m_yieldfrom = %(yield_from)s;
%(object_name)s->m_awaiting = true;
return NULL;
""" % {
        "object_name" : context.getContextObjectName(),
        "yield_from"  : iter_name,
    }

    resume_code = """\
%(object_name)s->m_awaiting = false;
""" % {
        "object_name" : context.getContextObjectName(),
    }

    _getYieldPreserveCode(
        to_name            = to_name,
        value_name         = (value_name, iter_name),
        yield_code         = yield_code,
        resume_code        = resume_code,
        preserve_exception = preserve_exception,
        conversion_check   = decideConversionCheckNeeded(to_name, expression),
        emit               = emit,
        context            = context
    )

    getErrorExitCode(
        check_name = to_name,
        emit       = emit,
        context    = context
    )

    context.addCleanupTempName(to_name)


def generateAsyncIterCode(to_name, expression, emit, context):
    value_name, = generateChildExpressionsCode(
        expression = expression,
        emit       = emit,
        context    = context
    )

    # In handlers, we must preserve/restore the exception.

    preserve_exception = False
    # TODO: Really?
    # preserve_exception = expression.isExceptionPreserving()

    iter_name = context.allocateTempName("async_iter")

    emit(
        "%s = ASYNC_MAKE_ITERATOR( %s );" % (
            iter_name,
            value_name
        )
    )

    getErrorExitCode(
        check_name   = iter_name,
        release_name = value_name,
        emit         = emit,
        context      = context
    )

    assert not context.needsCleanup(iter_name)

    yield_code = """\
%(object_name)s->m_yieldfrom = %(yield_from)s;
return NULL;
""" % {
        "object_name"      : context.getContextObjectName(),
        "yield_from"       : iter_name,
    }

    _getYieldPreserveCode(
        to_name            = to_name,
        value_name         = (value_name, iter_name),
        yield_code         = yield_code,
        resume_code        = None,
        preserve_exception = preserve_exception,
        conversion_check   = decideConversionCheckNeeded(to_name, expression),
        emit               = emit,
        context            = context
    )

    getErrorExitCode(
        check_name = to_name,
        emit       = emit,
        context    = context
    )

    context.addCleanupTempName(to_name)


def generateAsyncNextCode(to_name, expression, emit, context):
    value_name, = generateChildExpressionsCode(
        expression = expression,
        emit       = emit,
        context    = context
    )

    # In handlers, we must preserve/restore the exception.

    preserve_exception = False
    # TODO: Really?
    # preserve_exception = expression.isExceptionPreserving()

    iter_name = context.allocateTempName("async_next")

    emit(
        "%s = ASYNC_ITERATOR_NEXT( %s );" % (
            iter_name,
            value_name
        )
    )

    getErrorExitCode(
        check_name   = iter_name,
        release_name = value_name,
        emit         = emit,
        context      = context
    )

    assert not context.needsCleanup(iter_name)

    yield_code = """\
%(object_name)s->m_yieldfrom = %(yield_from)s;
return NULL;
""" % {
        "object_name"      : context.getContextObjectName(),
        "yield_from"       : iter_name,
    }

    _getYieldPreserveCode(
        to_name            = to_name,
        value_name         = (value_name, iter_name),
        yield_code         = yield_code,
        resume_code        = None,
        preserve_exception = preserve_exception,
        conversion_check   = decideConversionCheckNeeded(to_name, expression),
        emit               = emit,
        context            = context
    )

    getErrorExitCode(
        check_name = to_name,
        emit       = emit,
        context    = context
    )

    context.addCleanupTempName(to_name)
