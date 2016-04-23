import collections
import functools

from ..error import GraphQLError
from ..language import ast
from ..language.parser import parse
from ..language.source import Source
from ..pyutils.default_ordered_dict import DefaultOrderedDict
from ..pyutils.aplus import Promise, is_thenable, promisify, promise_for_dict
from ..pyutils.defer import (Deferred, DeferredDict, DeferredList, defer,
                             succeed)
from ..type import (GraphQLEnumType, GraphQLInterfaceType, GraphQLList,
                    GraphQLNonNull, GraphQLObjectType, GraphQLScalarType,
                    GraphQLUnionType, GraphQLSchema)
from ..validation import validate
from .base import (ExecutionContext, ExecutionResult, ResolveInfo, Undefined,
                   collect_fields, default_resolve_fn, get_field_def,
                   get_operation_root_type)



def execute(schema, document_ast, root_value=None, context_value=None, variable_values=None, operation_name=None):
    assert schema, 'Must provide schema'
    assert isinstance(schema, GraphQLSchema), 'Schema must be an instance of GraphQLSchema. Also ensure that there are not multiple versions of GraphQL installed in your node_modules directory.'

    context = ExecutionContext(
        schema,
        document_ast,
        root_value,
        context_value,
        variable_values,
        operation_name
    )

    def executor(resolve, reject):
        return resolve(execute_operation(context, context.operation, root_value))

    def on_rejected(error):
        context.errors.append(error)
        return None

    def on_resolve(data):
        return ExecutionResult(data=data, errors=context.errors)

    return Promise(executor).catch(on_rejected).then(on_resolve).value


def execute_operation(exe_context, operation, root_value):
    type = get_operation_root_type(exe_context.schema, operation)
    fields = collect_fields(
        exe_context,
        type,
        operation.selection_set,
        DefaultOrderedDict(list),
        set()
    )

    if operation.operation == 'mutation':
        return execute_fields_serially(exe_context, type, root_value, fields)

    return execute_fields(exe_context, type, root_value, fields)


def execute_fields_serially(exe_context, parent_type, source_value, fields):
    def execute_field_callback(results, response_name):
        field_asts = fields[response_name]
        result = resolve_field(
            exe_context,
            parent_type,
            source_value,
            field_asts
        )
        if result is Undefined:
            return results

        if is_thenable(result):
            def collect_result(resolved_result):
                results[response_name] = resolved_result
                return results

            return promisify(result).then(collect_result, None)

        results[response_name] = result
        return results
    def execute_field(prev_promise, response_name):
        return prev_promise.then(lambda results: execute_field_callback(results, response_name))

    return functools.reduce(execute_field, fields.keys(), Promise.resolve(collections.OrderedDict()))


# def execute_fields_serially(exe_context, parent_type, source_value, fields):
#     final_results = collections.OrderedDict()

#     prev_promise = Promise.resolve(collections.OrderedDict())

#     def on_promise(results, response_name, field_asts):
#         result = resolve_field(exe_context, parent_type, source_value, field_asts)
#         if result is Undefined:
#             return results

#         if is_thenable(result):
#             def collect_result(resolved_result):
#                 results[response_name] = resolved_result
#                 return results

#             return promisify(result).then(collect_result)

#         results[response_name] = result
#         return results

#     for response_name, field_asts in fields.items():
#         prev_promise = prev_promise.then(lambda results: on_promise(results, response_name, field_asts))

#     return prev_promise


def execute_fields(exe_context, parent_type, source_value, fields):
    contains_promise = False

    final_results = collections.OrderedDict()

    for response_name, field_asts in fields.items():
        result = resolve_field(exe_context, parent_type, source_value, field_asts)
        if result is Undefined:
            continue

        final_results[response_name] = result
        if is_thenable(result):
            contains_promise = True

    if not contains_promise:
        return final_results

    return promise_for_dict(final_results)


def resolve_field(exe_context, parent_type, source, field_asts):
    field_ast = field_asts[0]
    field_name = field_ast.name.value

    field_def = get_field_def(exe_context.schema, parent_type, field_name)
    if not field_def:
        return Undefined

    return_type = field_def.type
    resolve_fn = field_def.resolver or default_resolve_fn

    # Build a dict of arguments from the field.arguments AST, using the variables scope to
    # fulfill any variable references.
    args = exe_context.get_argument_values(field_def, field_ast)

    # The resolve function's optional third argument is a collection of
    # information about the current execution state.
    info = ResolveInfo(
        field_name,
        field_asts,
        return_type,
        parent_type,
        schema=exe_context.schema,
        fragments=exe_context.fragments,
        root_value=exe_context.root_value,
        operation= exe_context.operation,
        variable_values= exe_context.variable_values,
    )

    result = resolve_or_error(resolve_fn, source, args, exe_context, info)

    return complete_value_catching_error(
        exe_context,
        return_type,
        field_asts,
        info,
        result
    )


def resolve_or_error(resolve_fn, source, args, exe_context, info):
    try:
        # return resolve_fn(source, args, exe_context, info)
        return resolve_fn(source, args, info)
    except Exception as e:
        return e


def complete_value_catching_error(exe_context, return_type, field_asts, info, result):
    # If the field type is non-nullable, then it is resolved without any
    # protection from errors.
    if isinstance(return_type, GraphQLNonNull):
        return complete_value(exe_context, return_type, field_asts, info, result)

    # Otherwise, error protection is applied, logging the error and
    # resolving a null value for this field if one is encountered.
    try:
        completed = complete_value(exe_context, return_type, field_asts, info, result)
        if is_thenable(completed):
            def handle_error(error):
                exe_context.errors.append(error)
                return Promise.fulfilled(None)

            return promisify(completed).then(None, handle_error)

        return completed
    except Exception as e:
        exe_context.errors.append(e)
        return None


def complete_value(exe_context, return_type, field_asts, info, result):
    """
    Implements the instructions for completeValue as defined in the
    "Field entries" section of the spec.

    If the field type is Non-Null, then this recursively completes the value for the inner type. It throws a field
    error if that completion returns null, as per the "Nullability" section of the spec.

    If the field type is a List, then this recursively completes the value for the inner type on each item in the
    list.

    If the field type is a Scalar or Enum, ensures the completed value is a legal value of the type by calling the
    `serialize` method of GraphQL type definition.

    If the field is an abstract type, determine the runtime type of the value and then complete based on that type.

    Otherwise, the field type expects a sub-selection set, and will complete the value by evaluating all
    sub-selections.
    """
    # If field type is NonNull, complete for inner type, and throw field error if result is null.

    if is_thenable(result):
        return promisify(result).then(
            lambda resolved: complete_value(
                exe_context,
                return_type,
                field_asts,
                info,
                resolved
            ),
            lambda error: Promise.rejected(GraphQLError(error and str(error), field_asts, error))
        )

    if isinstance(result, Exception):
        raise GraphQLError(str(result), field_asts, result)

    if isinstance(return_type, GraphQLNonNull):
        completed = complete_value(
            exe_context, return_type.of_type, field_asts, info, result
        )
        if completed is None:
            raise GraphQLError(
                'Cannot return null for non-nullable field {}.{}.'.format(info.parent_type, info.field_name),
                field_asts
            )

        return completed

    # If result is null-like, return null.
    if result is None:
        return None

    # If field type is List, complete each item in the list with the inner type
    if isinstance(return_type, GraphQLList):
        return complete_list_value(exe_context, return_type, field_asts, info, result)

    # If field type is Scalar or Enum, serialize to a valid value, returning null if coercion is not possible.
    if isinstance(return_type, (GraphQLScalarType, GraphQLEnumType)):
        return complete_leaf_value(return_type, result)

    if isinstance(return_type, (GraphQLInterfaceType, GraphQLUnionType)):
        return complete_abstract_value(exe_context, return_type, field_asts, info, result)

    if isinstance(return_type, GraphQLObjectType):
        return complete_object_value(exe_context, return_type, field_asts, info, result)

    assert False, u'Cannot complete value of unexpected type "{}".'.format(return_type)


def complete_list_value(exe_context, return_type, field_asts, info, result):
    """
    Complete a list value by completing each item in the list with the inner type
    """
    assert isinstance(result, collections.Iterable), \
        ('User Error: expected iterable, but did not find one ' +
         'for field {}.{}.').format(info.parent_type, info.field_name)

    item_type = return_type.of_type
    completed_results = []
    contains_promise = False
    for item in result:
        completed_item = complete_value_catching_error(exe_context, item_type, field_asts, info, item)
        if not contains_promise and is_thenable(completed_item):
            contains_promise = True

        completed_results.append(completed_item)

    return Promise.all(completed_results) if contains_promise else completed_results



def complete_leaf_value(return_type, result):
    """
    Complete a Scalar or Enum by serializing to a valid value, returning null if serialization is not possible.
    """
    serialize = getattr(return_type, 'serialize', None)
    assert serialize, 'Missing serialize method on type'

    serialized_result = serialize(result)

    if serialized_result is None:
        return None

    return serialized_result


# TODO: Refactor based on js implementation
def complete_abstract_value(exe_context, return_type, field_asts, info, result):
    """
    Complete an value of an abstract type by determining the runtime type of that value, then completing based
    on that type.
    """
    # Field type must be Object, Interface or Union and expect sub-selections.
    runtime_type = None

    if isinstance(return_type, (GraphQLInterfaceType, GraphQLUnionType)):
        runtime_type = return_type.resolve_type(result, info)
        if runtime_type and not return_type.is_possible_type(runtime_type):
            raise GraphQLError(
                u'Runtime Object type "{}" is not a possible type for "{}".'.format(runtime_type, return_type),
                field_asts
            )

    if not runtime_type:
        return None

    return complete_object_value(exe_context, runtime_type, field_asts, info, result)


def complete_object_value(exe_context, return_type, field_asts, info, result):
    """
    Complete an Object value by evaluating all sub-selections.
    """
    if return_type.is_type_of and not return_type.is_type_of(result, info):
        raise GraphQLError(
            u'Expected value of type "{}" but got: {}.'.format(return_type, type(result).__name__),
            field_asts
        )

    # Collect sub-fields to execute to complete this value.
    subfield_asts = DefaultOrderedDict(list)
    visited_fragment_names = set()
    for field_ast in field_asts:
        selection_set = field_ast.selection_set
        if selection_set:
            subfield_asts = collect_fields(
                exe_context, return_type, selection_set,
                subfield_asts, visited_fragment_names
            )

    return execute_fields(exe_context, return_type, result, subfield_asts)
