import datetime

from collections import OrderedDict

import regex as re

import six

import pandas as pd

from google.api_core.exceptions import NotFound
import google.cloud.bigquery as bq

from multipledispatch import Dispatcher

import ibis
import ibis.common as com
import ibis.expr.operations as ops
import ibis.expr.types as ir
import ibis.expr.schema as sch
import ibis.expr.datatypes as dt
import ibis.expr.lineage as lin

from ibis.compat import parse_version
from ibis.client import Database, Query, SQLClient
from ibis.bigquery import compiler as comp

NATIVE_PARTITION_COL = '_PARTITIONTIME'


_IBIS_TYPE_TO_DTYPE = {
    'string': 'STRING',
    'int64': 'INT64',
    'double': 'FLOAT64',
    'boolean': 'BOOL',
    'timestamp': 'TIMESTAMP',
    'date': 'DATE',
}

_DTYPE_TO_IBIS_TYPE = {
    'INT64': dt.int64,
    'FLOAT64': dt.double,
    'BOOL': dt.boolean,
    'STRING': dt.string,
    'DATE': dt.date,
    # FIXME: enforce no tz info
    'DATETIME': dt.timestamp,
    'TIME': dt.time,
    'TIMESTAMP': dt.timestamp,
    'BYTES': dt.binary,
}


_LEGACY_TO_STANDARD = {
    'INTEGER': 'INT64',
    'FLOAT': 'FLOAT64',
    'BOOLEAN': 'BOOL',
}


@dt.dtype.register(bq.schema.SchemaField)
def bigquery_field_to_ibis_dtype(field):
    typ = field.field_type
    if typ == 'RECORD':
        fields = field.fields
        assert fields, 'RECORD fields are empty'
        names = [el.name for el in fields]
        ibis_types = list(map(dt.dtype, fields))
        ibis_type = dt.Struct(names, ibis_types)
    else:
        ibis_type = _LEGACY_TO_STANDARD.get(typ, typ)
        ibis_type = _DTYPE_TO_IBIS_TYPE.get(ibis_type, ibis_type)
    if field.mode == 'REPEATED':
        ibis_type = dt.Array(ibis_type)
    return ibis_type


@sch.infer.register(bq.table.Table)
def bigquery_schema(table):
    fields = OrderedDict((el.name, dt.dtype(el)) for el in table.schema)
    partition_info = table._properties.get('timePartitioning', None)

    # We have a partitioned table
    if partition_info is not None:
        partition_field = partition_info.get('field', NATIVE_PARTITION_COL)

        # Only add a new column if it's not already a column in the schema
        fields.setdefault(partition_field, dt.timestamp)
    return sch.schema(fields)


class BigQueryCursor(object):
    """Cursor to allow the BigQuery client to reuse machinery in ibis/client.py
    """

    def __init__(self, query):
        self.query = query

    def fetchall(self):
        result = self.query.result()
        return [row.values() for row in result]

    @property
    def columns(self):
        result = self.query.result()
        return [field.name for field in result.schema]

    def __enter__(self):
        # For compatibility when constructed from Query.execute()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        pass


def _find_scalar_parameter(expr):
    """:func:`~ibis.expr.lineage.traverse` function to find all
    :class:`~ibis.expr.types.ScalarParameter` instances and yield the operation
    and the parent expresssion's resolved name.

    Parameters
    ----------
    expr : ibis.expr.types.Expr

    Returns
    -------
    Tuple[bool, object]
    """
    op = expr.op()

    if isinstance(op, ops.ScalarParameter):
        result = op, expr.get_name()
    else:
        result = None
    return lin.proceed, result


class BigQueryQuery(Query):

    def __init__(self, client, ddl, query_parameters=None):
        super(BigQueryQuery, self).__init__(client, ddl)

        # self.expr comes from the parent class
        query_parameter_names = dict(
            lin.traverse(_find_scalar_parameter, self.expr))
        self.query_parameters = [
            bigquery_param(
                param.to_expr().name(query_parameter_names[param]), value
            ) for param, value in (query_parameters or {}).items()
        ]

    def _fetch(self, cursor):
        df = cursor.query.to_dataframe()
        return self.schema().apply_to(df)

    def execute(self):
        # synchronous by default
        with self.client._execute(
            self.compiled_sql,
            results=True,
            query_parameters=self.query_parameters
        ) as cur:
            result = self._fetch(cur)

        return self._wrap_result(result)


class BigQueryDatabase(Database):
    pass


bigquery_param = Dispatcher('bigquery_param')


@bigquery_param.register(ir.StructScalar, OrderedDict)
def bq_param_struct(param, value):
    field_params = [bigquery_param(param[k], v) for k, v in value.items()]
    return bq.StructQueryParameter(param.get_name(), *field_params)


@bigquery_param.register(ir.ArrayValue, list)
def bq_param_array(param, value):
    param_type = param.type()
    assert isinstance(param_type, dt.Array), str(param_type)

    try:
        bigquery_type = _IBIS_TYPE_TO_DTYPE[str(param_type.value_type)]
    except KeyError:
        raise com.UnsupportedBackendType(param_type)
    else:
        return bq.ArrayQueryParameter(param.get_name(), bigquery_type, value)


@bigquery_param.register(
    ir.TimestampScalar,
    six.string_types + (datetime.datetime, datetime.date)
)
def bq_param_timestamp(param, value):
    assert isinstance(param.type(), dt.Timestamp), str(param.type())

    # TODO(phillipc): Not sure if this is the correct way to do this.
    timestamp_value = pd.Timestamp(value, tz='UTC').to_pydatetime()
    return bq.ScalarQueryParameter(
        param.get_name(), 'TIMESTAMP', timestamp_value)


@bigquery_param.register(ir.StringScalar, six.string_types)
def bq_param_string(param, value):
    return bq.ScalarQueryParameter(param.get_name(), 'STRING', value)


@bigquery_param.register(ir.IntegerScalar, six.integer_types)
def bq_param_integer(param, value):
    return bq.ScalarQueryParameter(param.get_name(), 'INT64', value)


@bigquery_param.register(ir.FloatingScalar, float)
def bq_param_double(param, value):
    return bq.ScalarQueryParameter(param.get_name(), 'FLOAT64', value)


@bigquery_param.register(ir.BooleanScalar, bool)
def bq_param_boolean(param, value):
    return bq.ScalarQueryParameter(param.get_name(), 'BOOL', value)


@bigquery_param.register(ir.DateScalar, six.string_types)
def bq_param_date_string(param, value):
    return bigquery_param(param, pd.Timestamp(value).to_pydatetime().date())


@bigquery_param.register(ir.DateScalar, datetime.datetime)
def bq_param_date_datetime(param, value):
    return bigquery_param(param, value.date())


@bigquery_param.register(ir.DateScalar, datetime.date)
def bq_param_date(param, value):
    return bq.ScalarQueryParameter(param.get_name(), 'DATE', value)


class BigQueryTable(ops.DatabaseTable):
    pass


def rename_partitioned_column(table_expr, bq_table):
    partition_info = bq_table._properties.get('timePartitioning', None)

    # If we don't have any partiton information, the table isn't partitioned
    if partition_info is None:
        return table_expr

    # If we have a partition, but no "field" field in the table properties,
    # then use NATIVE_PARTITION_COL as the default
    partition_field = partition_info.get('field', NATIVE_PARTITION_COL)

    # The partition field must be in table_expr columns
    assert partition_field in table_expr.columns

    # User configured partition column name default
    col = ibis.options.bigquery.partition_col

    # No renaming if the config option is set to None
    if col is None:
        return table_expr
    return table_expr.relabel({partition_field: col})


def parse_project_and_dataset(project, dataset):
    """Figure out the project id under which queries will run versus the
    project of where the data live as well as what dataset to use.

    Parameters
    ----------
    project : str
        A project name
    dataset : str
        A ``<project>.<dataset>`` string or just a dataset name

    Returns
    -------
    data_project, billing_project, dataset : str, str, str

    Examples
    --------
    >>> data_project, billing_project, dataset = parse_project_and_dataset(
    ...     'ibis-gbq',
    ...     'foo-bar.my_dataset'
    ... )
    >>> data_project
    'foo-bar'
    >>> billing_project
    'ibis-gbq'
    >>> dataset
    'my_dataset'
    >>> data_project, billing_project, dataset = parse_project_and_dataset(
    ...     'ibis-gbq',
    ...     'my_dataset'
    ... )
    >>> data_project
    'ibis-gbq'
    >>> billing_project
    'ibis-gbq'
    >>> dataset
    'my_dataset'
    """
    try:
        data_project, dataset = dataset.split('.')
    except ValueError:
        billing_project = data_project = project
    else:
        billing_project = project
    return data_project, billing_project, dataset


class BigQueryClient(SQLClient):

    sync_query = BigQueryQuery
    database_class = BigQueryDatabase
    table_class = BigQueryTable
    dialect = comp.BigQueryDialect

    def __init__(self, project_id, dataset_id):
        """
        Parameters
        ----------
        project_id : str
            A project name
        dataset_id : str
            A ``<project_id>.<dataset_id>`` string or just a dataset name
        """
        (self.data_project,
         self.billing_project,
         self.dataset) = parse_project_and_dataset(project_id, dataset_id)
        self.client = bq.Client(project=self.data_project)

    def _parse_project_and_dataset(self, dataset):
        project, _, dataset = parse_project_and_dataset(
            self.billing_project,
            dataset or '{}.{}'.format(self.data_project, self.dataset),
        )
        return project, dataset

    @property
    def project_id(self):
        return self.data_project

    @property
    def dataset_id(self):
        return self.dataset

    def table(self, name, database=None):
        t = super(BigQueryClient, self).table(name, database=database)
        project, dataset, name = t.op().name.split('.')
        dataset_ref = self.client.dataset(dataset, project=project)
        table_ref = dataset_ref.table(name)
        bq_table = self.client.get_table(table_ref)
        return rename_partitioned_column(t, bq_table)

    def _build_ast(self, expr, context):
        result = comp.build_ast(expr, context)
        return result

    def _execute_query(self, dml, async=False):
        if async:
            raise NotImplementedError(
                'Asynchronous queries not implemented in the BigQuery backend'
            )
        klass = self.async_query if async else self.sync_query
        inst = klass(self, dml, query_parameters=dml.context.params)
        df = inst.execute()
        return df

    def _fully_qualified_name(self, name, database):
        project, dataset = self._parse_project_and_dataset(database)
        return '{}.{}.{}'.format(project, dataset, name)

    def _get_table_schema(self, qualified_name):
        dataset, table = qualified_name.rsplit('.', 1)
        return self.get_schema(table, database=dataset)

    def _execute(self, stmt, results=True, query_parameters=None):
        job_config = bq.job.QueryJobConfig()
        job_config.query_parameters = query_parameters or []
        job_config.use_legacy_sql = False  # False by default in >=0.28
        query = self.client.query(
            stmt, job_config=job_config, project=self.billing_project
        )
        query.result()  # blocks until finished
        return BigQueryCursor(query)

    def database(self, name=None):
        return self.database_class(name or self.dataset, self)

    @property
    def current_database(self):
        return self.database(self.dataset)

    def set_database(self, name):
        self.data_project, self.dataset = self._parse_project_and_dataset(name)

    def exists_database(self, name):
        project, dataset = self._parse_project_and_dataset(name)
        client = self.client
        dataset_ref = client.dataset(dataset, project=project)
        try:
            client.get_dataset(dataset_ref)
        except NotFound:
            return False
        else:
            return True

    def list_databases(self, like=None):
        results = [
            dataset.dataset_id for dataset in self.client.list_datasets()
        ]
        if like:
            results = [
                dataset_name for dataset_name in results
                if re.match(like, dataset_name) is not None
            ]
        return results

    def exists_table(self, name, database=None):
        project, dataset = self._parse_project_and_dataset(database)
        client = self.client
        dataset_ref = self.client.dataset(dataset, project=project)
        table_ref = dataset_ref.table(name)
        try:
            client.get_table(table_ref)
        except NotFound:
            return False
        else:
            return True

    def list_tables(self, like=None, database=None):
        project, dataset = self._parse_project_and_dataset(database)
        dataset_ref = self.client.dataset(dataset, project=project)
        result = [
            table.table_id for table in self.client.list_tables(dataset_ref)
        ]
        if like:
            result = [
                table_name for table_name in result
                if re.match(like, table_name) is not None
            ]
        return result

    def get_schema(self, name, database=None):
        project, dataset = self._parse_project_and_dataset(database)
        table_ref = self.client.dataset(dataset, project=project).table(name)
        bq_table = self.client.get_table(table_ref)
        return sch.infer(bq_table)

    @property
    def version(self):
        return parse_version(bq.__version__)
