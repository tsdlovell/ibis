import numpy as np
import pandas as pd

from multipledispatch import Dispatcher

import ibis.common as com
import ibis.util as util
import ibis.expr.datatypes as dt


class Schema(object):

    """An object for holding table schema information, i.e., column names and
    types.

    Parameters
    ----------
    names : Sequence[str]
        A sequence of ``str`` indicating the name of each column.
    types : Sequence[DataType]
        A sequence of :class:`ibis.expr.datatypes.DataType` objects
        representing type of each column.
    """

    __slots__ = 'names', 'types', '_name_locs'

    def __init__(self, names, types):
        if not isinstance(names, list):
            names = list(names)

        self.names = names
        self.types = list(map(dt.dtype, types))

        self._name_locs = dict((v, i) for i, v in enumerate(self.names))

        if len(self._name_locs) < len(self.names):
            raise com.IntegrityError('Duplicate column names')

    def __repr__(self):
        space = 2 + max(map(len, self.names))
        return "ibis.Schema {{{}\n}}".format(
            util.indent(
                ''.join(
                    '\n{}{}'.format(name.ljust(space), str(type))
                    for name, type in zip(self.names, self.types)
                ),
                2
            )
        )

    def __hash__(self):
        return hash((type(self), tuple(self.names), tuple(self.types)))

    def __len__(self):
        return len(self.names)

    def __iter__(self):
        return iter(self.names)

    def __contains__(self, name):
        return name in self._name_locs

    def __getitem__(self, name):
        return self.types[self._name_locs[name]]

    def __getstate__(self):
        return {
            slot: getattr(self, slot) for slot in self.__class__.__slots__
        }

    def __setstate__(self, instance_dict):
        for key, value in instance_dict.items():
            setattr(self, key, value)

    def delete(self, names_to_delete):
        for name in names_to_delete:
            if name not in self:
                raise KeyError(name)

        new_names, new_types = [], []
        for name, type_ in zip(self.names, self.types):
            if name in names_to_delete:
                continue
            new_names.append(name)
            new_types.append(type_)

        return Schema(new_names, new_types)

    @classmethod
    def from_tuples(cls, values):
        if not isinstance(values, (list, tuple)):
            values = list(values)

        names, types = zip(*values) if values else ([], [])
        return Schema(names, types)

    @classmethod
    def from_dict(cls, dictionary):
        return Schema(*zip(*dictionary.items()))

    def equals(self, other, cache=None):
        return self.names == other.names and self.types == other.types

    def __eq__(self, other):
        return self.equals(other)

    def append(self, schema):
        return Schema(self.names + schema.names, self.types + schema.types)

    def items(self):
        return zip(self.names, self.types)

    def name_at_position(self, i):
        """
        """
        upper = len(self.names) - 1
        if not 0 <= i <= upper:
            raise ValueError(
                'Column index must be between 0 and {:d}, inclusive'.format(
                    upper
                )
            )
        return self.names[i]


class HasSchema(object):

    """
    Base class representing a structured dataset with a well-defined
    schema.

    Base implementation is for tables that do not reference a particular
    concrete dataset or database table.
    """

    def __init__(self, schema, name=None):
        if not isinstance(schema, Schema):
            raise TypeError(
                'schema argument to HasSchema class must be a Schema instance'
            )
        self.schema = schema
        self.name = name

    def __repr__(self):
        return '{}({})'.format(type(self).__name__, repr(self.schema))

    def has_schema(self):
        return True

    def equals(self, other, cache=None):
        return type(self) == type(other) and self.schema.equals(
            other.schema, cache=cache
        )

    def root_tables(self):
        return [self]


schema = Dispatcher('schema')


@schema.register(Schema)
def identity(s):
    return s


@schema.register(pd.Series)
def schema_from_series(s):
    return Schema.from_tuples(s.iteritems())


@schema.register((tuple, list))
def schema_from_list(lst):
    return Schema.from_tuples(lst)


@schema.register(dict)
def schema_from_dict(d):
    return Schema.from_dict(d)


infer = Dispatcher('infer')


try:
    infer_pandas_dtype = pd.api.types.infer_dtype
except AttributeError:
    infer_pandas_dtype = pd.lib.infer_dtype


PANDAS_DTYPE_TO_IBIS_DTYPE = {
    'string': dt.string,
    'unicode': dt.string,
    'empty': dt.null,
    'boolean': dt.boolean,
    'datetime': dt.timestamp,
    'datetime64': dt.timestamp,
    'timedelta': dt.interval,
    'bytes': dt.binary,
}


def infer_ibis_dtypes_from_series(series, strict, aggressive_null):

    def _infer_ibis_dtypes_from_series_strict(series_nona):
        ibis_dtypes = []
        try:
            ibis_dtypes = series_nona.map(dt.infer).unique()
            ibis_dtypes = [dt.highest_precedence(ibis_dtypes)]
        except com.IbisTypeError:
            pass
        return ibis_dtypes

    if aggressive_null:
        series = series.dropna()
        if not len(series):
            return [dt.null]

    if series.dtype != np.object_:
        ibis_dtype = dt.dtype(series.dtype)
    else:
        series_nona = series.dropna()
        pandas_dtype = infer_pandas_dtype(series_nona)
        if pandas_dtype.startswith('mixed'):
            if strict:
                return _infer_ibis_dtypes_from_series_strict(series_nona)
            else:
                ibis_dtype = dt.infer(series_nona.iat[0])
        else:
            ibis_dtype = PANDAS_DTYPE_TO_IBIS_DTYPE.get(pandas_dtype)
            if ibis_dtype is None:
                # FIXME: add this case to PANDAS_DTYPE_TO_IBIS_DTYPE
                return []
    return [ibis_dtype]


@infer.register(pd.DataFrame)
def infer_pandas_schema(df, strict=True, aggressive_null=True):
    pairs = [
        (col, infer_ibis_dtypes_from_series(series, strict, aggressive_null))
        for (col, series) in df.iteritems()
    ]
    none_cols = [col for (col, dtypes) in pairs if len(dtypes) == 0]
    multi_cols = [(col, dtypes) for (col, dtypes) in pairs if len(dtypes) > 1]
    pairs = [(col, dtypes[0]) for (col, dtypes) in pairs if len(dtypes) == 1]
    if none_cols or multi_cols:
        msg = ''
        if none_cols:
            infix = '\n\t' + ',\n\t'.join(
                '{}: <explicit type>'.format(col) for col in none_cols)
            msg += (
                'Unable to infer type of column(s) {0!r}. Try instantiating '
                'your table from the client with\n'
                'client.table('
                "'my_table', schema={{{1}}})"
                .format(none_cols, infix)
            )
        if none_cols and multi_cols:
            msg += '\n'
        if multi_cols:
            postfix = '\n'.join(
                '{}:\n\t{}'.format(col, '\n\t'.join(map(str, typs)))
                for (col, typs) in multi_cols
            )
            msg += (
                'Multiple types found for columns(s):\n' + postfix
            )
        raise TypeError(msg)
    else:
        return Schema.from_tuples(pairs)
