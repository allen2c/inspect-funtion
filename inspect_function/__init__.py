# inspect_function/__init__.py
import asyncio
import inspect
import pathlib
import typing
from enum import StrEnum
from typing import Any, Awaitable, Union

import pydantic

__version__ = pathlib.Path(__file__).parent.joinpath("VERSION").read_text().strip()

P = typing.ParamSpec("P")


def inspect_function(
    func: typing.Callable[..., Union[Any, Awaitable[Any]]],
) -> "FunctionInspection":
    """
    Analyze a callable's signature and return comprehensive inspection details.
    Extracts parameter information, return type, and function characteristics
    including async nature, method type, and parameter kinds.
    """
    sig = inspect.signature(func)

    # Check if function is awaitable/coroutine
    awaitable = asyncio.iscoroutinefunction(func)

    # Detect function type using Python's built-in functions
    is_bound_method = inspect.ismethod(func)

    # For classmethods, we need to check the __func__ attribute if it exists
    is_classmethod_detected = False
    is_method_detected = False

    if is_bound_method:
        # This is a bound method - could be instance method or classmethod
        # Check if it's a classmethod by looking at the underlying function
        if hasattr(func, "__self__") and inspect.isclass(
            getattr(func, "__self__", None)
        ):
            # Bound to a class, this is a classmethod
            is_classmethod_detected = True
        else:
            # Bound to an instance, this is an instance method
            is_method_detected = True
    elif hasattr(func, "__func__"):
        # This might be an unbound classmethod
        if hasattr(func, "__self__") and inspect.isclass(
            getattr(func, "__self__", None)
        ):
            is_classmethod_detected = True
    else:
        # Check if it's an unbound instance method by looking at parameter names
        # (only as fallback when we have the signature available)
        if len(sig.parameters) > 0:
            first_param = list(sig.parameters.keys())[0]
            if first_param == "self":
                is_method_detected = True
            elif first_param == "cls":
                is_classmethod_detected = True

    # Process parameters
    parameters = []
    for i, (name, param) in enumerate(sig.parameters.items()):
        # Map inspect.Parameter.kind to our ParameterKind
        kind_mapping = {
            inspect.Parameter.POSITIONAL_ONLY: ParameterKind.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD: (
                ParameterKind.POSITIONAL_OR_KEYWORD
            ),
            inspect.Parameter.VAR_POSITIONAL: ParameterKind.VAR_POSITIONAL,
            inspect.Parameter.KEYWORD_ONLY: ParameterKind.KEYWORD_ONLY,
            inspect.Parameter.VAR_KEYWORD: ParameterKind.VAR_KEYWORD,
        }

        # Get annotation as string
        annotation = (
            str(param.annotation)
            if param.annotation != inspect.Parameter.empty
            else "Any"
        )

        # Handle default values
        has_default = param.default != inspect.Parameter.empty
        default_value = repr(param.default) if has_default else None

        # Determine if parameter is optional
        is_optional = has_default or param.kind in {
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        }

        # Only set position for non-variadic parameters
        position = (
            i
            if param.kind
            not in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}
            else None
        )

        param_obj = Parameter(
            name=name,
            kind=kind_mapping[param.kind],
            annotation=annotation,
            default_value=default_value,
            has_default=has_default,
            position=position,
            is_optional=is_optional,
        )
        parameters.append(param_obj)

    # Get return annotation
    return_annotation = (
        str(sig.return_annotation)
        if sig.return_annotation != inspect.Signature.empty
        else "Any"
    )

    return FunctionInspection(
        awaitable=awaitable,
        parameters=parameters,
        return_annotation=return_annotation,
        detected_as_method=is_method_detected,
        detected_as_classmethod=is_classmethod_detected,
    )


def inspect_parameters(
    func: typing.Callable[P, Union[Any, Awaitable[Any]]],
    parameters: typing.Dict[str, typing.Any],
) -> tuple[tuple[typing.Any, ...], dict[str, typing.Any]]:
    """
    Transform a parameter dictionary into properly ordered args and kwargs.
    Converts a function and parameter dict into positional args and keyword args
    that can be safely passed to the function.
    """  # noqa: E501

    func_inspection = inspect_function(func)

    positional_args = []
    keyword_args = {}
    used_params = set()

    # Process parameters in signature order to maintain proper positioning
    for param in func_inspection.parameters:
        if param.name not in parameters:
            continue

        param_value = parameters[param.name]
        used_params.add(param.name)

        # Handle different parameter kinds
        if param.kind == ParameterKind.POSITIONAL_ONLY:
            # Must be passed as positional argument
            positional_args.append(param_value)
        elif param.kind == ParameterKind.POSITIONAL_OR_KEYWORD:
            # For functions with *args, parameters before *args should be positional
            # to maintain correct order, unless there are keyword-only params
            has_var_positional = func_inspection.var_positional_param is not None
            if has_var_positional:
                # Check if this param comes before *args in signature
                var_pos_index = None
                param_index = None
                for i, p in enumerate(func_inspection.parameters):
                    if p.kind == ParameterKind.VAR_POSITIONAL:
                        var_pos_index = i
                    if p.name == param.name:
                        param_index = i

                if (
                    param_index is not None
                    and var_pos_index is not None
                    and param_index < var_pos_index
                ):
                    # This param comes before *args, so use positional
                    positional_args.append(param_value)
                else:
                    # This param comes after *args or no clear order, use keyword
                    keyword_args[param.name] = param_value
            else:
                # No *args, can use keyword
                keyword_args[param.name] = param_value
        elif param.kind == ParameterKind.KEYWORD_ONLY:
            # Must be passed as keyword argument
            keyword_args[param.name] = param_value
        elif param.kind == ParameterKind.VAR_POSITIONAL:
            # Special handling for *args - expand if it's a sequence
            if isinstance(param_value, (list, tuple)):
                positional_args.extend(param_value)
            else:
                # Treat as a single positional argument
                positional_args.append(param_value)
        elif param.kind == ParameterKind.VAR_KEYWORD:
            # Special handling for **kwargs - merge if it's a dict
            if isinstance(param_value, dict):
                keyword_args.update(param_value)
            else:
                # Treat as a single keyword argument with the parameter name
                keyword_args[param.name] = param_value

    # Handle any remaining parameters not in the function signature
    for param_name, param_value in parameters.items():
        if param_name in used_params:
            continue

        # Parameter not found in signature -
        # only add to kwargs if function accepts **kwargs
        var_keyword_param = func_inspection.var_keyword_param
        if var_keyword_param:
            keyword_args[param_name] = param_value
        # If no **kwargs parameter, ignore extra parameters

    return tuple(positional_args), keyword_args


class ParameterKind(StrEnum):
    """
    Enumeration of Python parameter types.
    Maps to Python's inspect.Parameter.kind values for different
    parameter declaration styles like positional-only and keyword-only.
    """

    POSITIONAL_ONLY = "positional_only"  # before /
    POSITIONAL_OR_KEYWORD = "positional_or_keyword"  # default
    VAR_POSITIONAL = "var_positional"  # *args
    KEYWORD_ONLY = "keyword_only"  # after *
    VAR_KEYWORD = "var_keyword"  # **kwargs


class Parameter(pydantic.BaseModel):
    """
    Detailed information about a single function parameter.
    Contains metadata including type annotation, default value,
    parameter kind, and position within the function signature.
    """

    name: str
    kind: ParameterKind
    annotation: str
    default_value: str | None = pydantic.Field(
        default=None, description="Default value of the parameter in repr()"
    )
    has_default: bool = pydantic.Field(
        default=False, description="Whether the parameter has a default value"
    )
    position: int | None = pydantic.Field(
        default=None, description="Parameter position in the signature"
    )
    is_optional: bool = pydantic.Field(
        default=False, description="Whether the parameter is optional"
    )


class FunctionInspection(pydantic.BaseModel):
    """
    Complete analysis of a function's signature and characteristics.
    Provides structured access to parameters, return type, and function
    properties including async nature and method classification.
    """

    awaitable: bool = pydantic.Field(
        ..., description="Whether the function is awaitable"
    )
    parameters: typing.List[Parameter] = pydantic.Field(
        default_factory=list, description="All parameters in signature order"
    )
    return_annotation: str
    detected_as_method: bool = pydantic.Field(
        default=False, description="Whether function was detected as an instance method"
    )
    detected_as_classmethod: bool = pydantic.Field(
        default=False, description="Whether function was detected as a class method"
    )

    @property
    def is_method(self) -> bool:
        """True if this is an instance method (has 'self' parameter)."""
        return self.detected_as_method

    @property
    def is_classmethod(self) -> bool:
        """True if this is a class method (has 'cls' parameter)."""
        return self.detected_as_classmethod

    @property
    def is_function(self) -> bool:
        """True if this is a regular function (not a method or classmethod)."""
        return not self.is_method and not self.is_classmethod

    @property
    def is_coroutine_function(self) -> bool:
        """True if this is an async function that returns a coroutine."""
        return self.awaitable

    @property
    def positional_only_params(self) -> typing.List[Parameter]:
        """Parameters that must be passed positionally (declared before /)."""
        return [p for p in self.parameters if p.kind == ParameterKind.POSITIONAL_ONLY]

    @property
    def positional_or_keyword_params(self) -> typing.List[Parameter]:
        """Parameters that can be passed either positionally or by keyword."""
        return [
            p for p in self.parameters if p.kind == ParameterKind.POSITIONAL_OR_KEYWORD
        ]

    @property
    def keyword_only_params(self) -> typing.List[Parameter]:
        """Parameters that must be passed by keyword (declared after *)."""
        return [p for p in self.parameters if p.kind == ParameterKind.KEYWORD_ONLY]

    @property
    def var_positional_param(self) -> Parameter | None:
        """The *args parameter if the function accepts variable positional arguments."""
        var_pos = [p for p in self.parameters if p.kind == ParameterKind.VAR_POSITIONAL]
        return var_pos[0] if var_pos else None

    @property
    def var_keyword_param(self) -> Parameter | None:
        """The **kwargs parameter if the function accepts variable keyword arguments."""
        var_kw = [p for p in self.parameters if p.kind == ParameterKind.VAR_KEYWORD]
        return var_kw[0] if var_kw else None

    @property
    def required_params(self) -> typing.List[Parameter]:
        """Parameters without default values that must be provided when calling."""
        return [
            p
            for p in self.parameters
            if not p.has_default
            and p.kind not in {ParameterKind.VAR_POSITIONAL, ParameterKind.VAR_KEYWORD}
        ]

    @property
    def optional_params(self) -> typing.List[Parameter]:
        """Parameters that have default values and are optional when calling."""
        return [p for p in self.parameters if p.has_default]

    @property
    def json_schema(self) -> typing.Dict[str, typing.Any]:
        """
        Generate OpenAPI-compatible JSON Schema for function parameters.
        Creates a schema describing parameter types, defaults, and requirements
        suitable for API documentation and validation.
        """

        from inspect_function.utils.get_openapi_type import get_openapi_type

        # Build properties for each parameter
        properties = {}
        required = []

        for param in self.parameters:
            # Skip 'self' and 'cls' parameters for methods
            if param.name in ("self", "cls"):
                continue

            # Handle different parameter kinds
            if param.kind == ParameterKind.VAR_POSITIONAL:
                # *args - represent as array
                properties[param.name] = {
                    "type": "array",
                    "items": {"type": "any"},
                    "description": f"Variable positional arguments (*{param.name})",
                }
            elif param.kind == ParameterKind.VAR_KEYWORD:
                # **kwargs - represent as object with additional properties
                properties[param.name] = {
                    "type": "object",
                    "additionalProperties": True,
                    "description": f"Variable keyword arguments (**{param.name})",
                }
            else:
                # Regular parameter
                param_schema = {
                    "type": get_openapi_type(param.annotation),
                    "description": f"Parameter '{param.name}' of kind "
                    f"{param.kind.value}",
                }

                if param.has_default and param.default_value is not None:
                    param_schema["default"] = param.default_value

                properties[param.name] = param_schema

                # Add to required if no default value and not optional
                if not param.has_default and param.kind not in {
                    ParameterKind.VAR_POSITIONAL,
                    ParameterKind.VAR_KEYWORD,
                }:
                    required.append(param.name)

        # Build the main schema
        schema = {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
            "x-function-metadata": {
                "awaitable": self.awaitable,
                "return_annotation": self.return_annotation,
                "is_method": self.is_method,
                "is_classmethod": self.is_classmethod,
                "is_coroutine_function": self.is_coroutine_function,
            },
        }

        # Add description based on function type
        if self.is_method:
            schema["description"] = "Parameters for instance method"
        elif self.is_classmethod:
            schema["description"] = "Parameters for class method"
        elif self.is_coroutine_function:
            schema["description"] = "Parameters for async function"
        else:
            schema["description"] = "Parameters for function"

        return schema
