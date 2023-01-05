__all__ = ["HDF5Dataclass"]

from dataclasses import dataclass, is_dataclass, fields

from pathlib import Path
from typing import IO, Any, Union, get_args, get_origin, TypeGuard, Type
import types
from typing_extensions import dataclass_transform

from pydantic import BaseModel
import numpy as np
import h5py

FileType = str | Path | IO[bytes]


def _is_primitive(obj_or_class: Any) -> bool:
    primitives = (int, float, str)
    if type(obj_or_class) == type:
        return obj_or_class in primitives
    else:
        return isinstance(obj_or_class, primitives)


def _is_union(T: type) -> bool:
    # One is for A | B, another is for Union[A, B]. Shrug.
    return get_origin(T) in (types.UnionType, Union)


def _is_optional(T: type) -> bool:
    if not _is_union(T):
        return False
    args = get_args(T)
    if len(args) != 2:
        return False
    T1, T2 = args
    return T1 != T2 and types.NoneType in (T1, T2)


def _extract_type_from_optional(T: type) -> type:
    assert _is_optional(T)
    T1, T2 = get_args(T)
    return T1 if T2 == types.NoneType else T2


def _is_optional_primitive(T: type) -> bool:
    return _is_optional(T) and _is_primitive(_extract_type_from_optional(T))


def _is_numpy_array(T: type) -> bool:
    return get_origin(T) == np.ndarray


def _is_class_serialisable(T: type) -> TypeGuard[Type["HDF5Dataclass"]]:
    return is_dataclass(T) and issubclass(T, HDF5Dataclass)


def _is_supported_dict(T: type) -> bool:
    if not get_origin(T) == dict:
        return False
    K, V = get_args(T)
    return _is_primitive(K) and _is_type_supported(V)


def _is_pydantic_model(T: type) -> TypeGuard[Type[BaseModel]]:
    # Annoying error without try...catch: "issubclass() arg 1 must be a class"
    try:
        return issubclass(T, BaseModel)
    except TypeError:
        return False


def _is_type_supported(T: type) -> bool:
    return (
        _is_primitive(T)
        or (_is_optional(T) and _is_type_supported(_extract_type_from_optional(T)))
        or _is_numpy_array(T)
        or _is_class_serialisable(T)
        or _is_supported_dict(T)
        or _is_pydantic_model(T)
    )


def _fields(T: type) -> dict[str, type]:
    assert is_dataclass(T)
    ret: dict[str, type] = {}
    for field in fields(T):
        ret[field.name] = field.type
    return ret


@dataclass_transform()
class HDF5Dataclass:
    serialisable_attrs: dict[str, type]

    def __init_subclass__(cls, **kwargs):
        dataclass_cls = dataclass(cls, **kwargs)
        serialisable_attrs = _fields(dataclass_cls)
        unsupported_attrs = [
            attr for attr, T in serialisable_attrs.items() if not _is_type_supported(T)
        ]
        assert (
            not unsupported_attrs
        ), f"Types of attributes {', '.join(unsupported_attrs)} are not supported!"

        dataclass_cls.serialisable_attrs = serialisable_attrs
        return dataclass_cls

    def to_hdf5(self, output: FileType | h5py.File | h5py.Group):
        def serialise_single(
            attr: str, val: Any, T: type, h5: h5py.File | h5py.Group
        ) -> None:
            if val is None:
                return

            if _is_primitive(T) or _is_optional_primitive(T):
                h5.attrs[attr] = val
            elif _is_pydantic_model(T):
                h5.attrs[attr] = val.json()
            # TODO: elif list - json?
            elif _is_numpy_array(T):
                h5.create_dataset(attr, data=val)
            elif _is_class_serialisable(T):
                grp = h5.create_group(attr)
                val.to_hdf5(output=grp)
            elif _is_supported_dict(T):
                _, V = get_args(T)
                grp = h5.create_group(attr)
                for k, v in val.items():
                    serialise_single(k, v, V, grp)
            else:
                raise Exception(f"Unsupported type of attribute '{attr}'")

        h5 = (
            output
            if isinstance(output, (h5py.File, h5py.Group))
            else h5py.File(output, "w")
        )

        for attr, T in self.serialisable_attrs.items():
            val = getattr(self, attr)
            serialise_single(attr, val, T, h5)

    @classmethod
    def from_hdf5(cls, input: FileType | h5py.File | h5py.Group):
        def deserialise_single(attr: str, T: type, h5: h5py.File | h5py.Group) -> Any:
            val = None
            if _is_primitive(T):
                val = h5.attrs.get(attr)
                assert (
                    val is not None
                ), f"Attribute '{attr}' marked as non-optional, but value is not present!"
            elif _is_optional_primitive(T):
                val = h5.attrs.get(attr)
            elif _is_pydantic_model(T):
                val = T.parse_raw(h5.attrs.get(attr))
            else:
                serialised = h5[attr]
                if isinstance(serialised, h5py.Dataset):
                    val = np.array(serialised)
                elif isinstance(serialised, h5py.Group):
                    assert _is_class_serialisable(T) or _is_supported_dict(T)
                    if _is_class_serialisable(T):
                        val = T.from_hdf5(serialised)
                    else:
                        # dict case
                        _, V = get_args(T)
                        val = {}
                        keys = (
                            serialised.attrs.keys()
                            if _is_primitive(V) or _is_optional_primitive(V)
                            else serialised.keys()
                        )
                        for key in keys:
                            val[key] = deserialise_single(key, V, serialised)
                else:
                    raise Exception("Unknown type of data in hdf5")
            return val

        h5 = (
            input
            if isinstance(input, (h5py.File, h5py.Group))
            else h5py.File(input, "r")
        )

        attrs = {}
        for attr, T in cls.serialisable_attrs.items():
            attrs[attr] = deserialise_single(attr, T, h5)
        return cls(**attrs)
