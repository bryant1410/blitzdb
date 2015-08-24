import abc
import six
import uuid
import pprint
from collections import defaultdict

from ...document import Document
from ..base import Backend as BaseBackend
from ..base import NotInTransaction,DoNotSerialize
from ..file.serializers import JsonSerializer
from .queryset import QuerySet
from .relations import ListProxy,ManyToManyProxy

from blitzdb.fields import (ForeignKeyField,
                            ManyToManyField,
                            CharField,
                            IntegerField,
                            TextField,
                            FloatField,
                            ListField,
                            BooleanField,
                            BinaryField,
                            DateField,
                            DateTimeField,
                            BaseField
                            )

from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import MetaData,Table,Column,ForeignKey,UniqueConstraint
from sqlalchemy.types import (Integer,
                              VARCHAR,
                              String,
                              Float,
                              Boolean,
                              Date,
                              DateTime,
                              Text,
                              LargeBinary,
                              Unicode)
from sqlalchemy.sql import select,insert,update,func,and_,or_,not_,expression,null
from sqlalchemy.ext.compiler import compiles

@compiles(DateTime, "sqlite")
def compile_binary_sqlite(type_, compiler, **kw):
    return "VARCHAR(64)"

"""
Base model for SQL backend:

Data storage:

Define a JSON column in th underlying database

Indexes:

Define additional columns in the table with a given type

M2M-Relationships: Let the user define them through helper documents
"""

def _get_value(obj,key):
    key_fragments = key.split(".")
    current_dict = obj
    for key_fragment in key_fragments:
        current_dict = current_dict[key_fragment]
    return current_dict

def _set_value(obj,key,value):
    key_fragments = key.split('.')
    current_dict = obj
    for key_fragment in key_fragments[:-1]:
        if not key_fragment in current_dict:
            current_dict[key_fragment] = {}
        current_dict = current_dict[key_fragment]
    current_dict[key_fragments[-1]] = value

class Backend(BaseBackend):

    """
    A SQL backend.

    :param db: An instance of a `sqlalchemy. 
    <http://www.sqlalchemy.org>`_ class

    Example usage:

    .. code-block:: python

        from sqlalchemy import create_engine
        from blitzdb.backends.sql import Backend as SQLBackend

        my_engine = create_engine(...)

        #create a new BlitzDB backend using a SQLAlchemy engine
        backend = SQLBackend(my_engine)
    """

    class Meta(BaseBackend.Meta):

        PkType = VARCHAR(64)

    def __init__(self, engine, table_postfix = '',create_schema = False,**kwargs):
        super(Backend, self).__init__(**kwargs)

        self._engine = engine
        self._collection_tables = {}

        self._index_tables = defaultdict(dict)
        self._relationship_tables = defaultdict(dict)
        self._index_fields = defaultdict(dict)
        self._list_indexes = defaultdict(dict)
        self._related_fields = defaultdict(dict)
        self._excluded_fields = defaultdict(dict)
        self._transaction = None

        self.table_postfix = table_postfix

        if create_schema:
            self.create_schema()

        self._conn = self._engine.connect()

        self.begin()

    @property
    def connection(self):
        return self._conn

    def get_field_type(self,field):
        m = {
            IntegerField : Integer,
            FloatField : Float,
            CharField : VARCHAR,
            TextField : Text,
            BooleanField: Boolean,
            BinaryField: LargeBinary,
            DateField: Date,
            DateTimeField: DateTime
        }
        for cls,t in m.items():
            if isinstance(field,cls):
                return t
        raise AttributeError("Invalid field type: %s" % field)

    def init_schema(self):

        def add_foreign_key_field(collection,key,field):
            field_name = key
            column_name = key.replace('.','_')
            self._excluded_fields[collection][field_name] = True
            if isinstance(field.related,six.string_types):
                related_collection = self.get_collection_for_cls_name(field.related)
            else:
                related_collection = self.get_collection_for_cls(field.related)
            related_class = self.get_cls_for_collection(related_collection)

            column = Column(column_name,self.Meta.PkType,ForeignKey('%s%s.pk' % (related_collection,self.table_postfix)),index=True,nullable = True if field.nullable else False)
            self._related_fields[collection][field_name] = {'field' : field,
                                                            'key' : key,
                                                            'column' : column_name,
                                                            'collection' : related_collection,
                                                            'class' : related_class
                                                            }
            return column

        def add_many_to_many_field(collection,key,field):

            if isinstance(field.related,(list,tuple)):
                raise AttributeError("Currently not supported!")

            field_name = key
            self._excluded_fields[collection][field_name] = True
            if isinstance(field.related,six.string_types):
                related_collection = self.get_collection_for_cls_name(field.related)
            else:
                related_collection = self.get_collection_for_cls(field.related)
            related_class = self.get_cls_for_collection(related_collection)
            relationship_name = "%s_%s" % (collection,related_collection)

            params = {'field' : field,
                      'key' : key,
                      'collection' : related_collection,
                      'class' : related_class
                     }
            extra_columns = [
                UniqueConstraint('pk_%s' % related_collection,'pk_%s' % collection)
                ]
            relationship_table = Table('%s%s' % (relationship_name,self.table_postfix),self._metadata,
                    Column('pk_%s' % related_collection,self.Meta.PkType,ForeignKey('%s%s.pk' % (related_collection,self.table_postfix)),index = True),
                    Column('pk_%s' % collection,self.Meta.PkType,ForeignKey('%s%s.pk' % (collection,self.table_postfix)),index = True),
                    *extra_columns
                )
            params['relationship_table'] = relationship_table
            self._relationship_tables[collection][field_name] = relationship_table
            self._related_fields[collection][field_name] = params

        def add_list_field(collection,key,field):
            self._excluded_fields[collection][key] = True
            column_name = key.replace('.','_')
            index_name = "%s_%s" % (collection,column_name)

            index_params = {'field' : field,
                            'key' : key,
                            'type' : self.get_field_type(field.type),
                            'column' : column_name}
            self._index_tables[collection][key] = Table('%s%s' % (index_name,self.table_postfix),self._metadata,
                    Column('pk',self.Meta.PkType,ForeignKey('%s%s.pk' % (collection,self.table_postfix)),index = True),
                    Column(column_name,index_params['type'],index = True),
                    UniqueConstraint('pk',key,name = 'unique_index')
                )
            self._list_indexes[key] = index_params
            self._index_fields[collection][key] = index_params

        def add_field(collection,key,field):
            self._excluded_fields[collection][key] = True
            column_name = key.replace('.','_')
            index_params = {'field' : field,
                            'key' : key,
                            'type' : self.get_field_type(field),
                            'column' : column_name}
            self._index_fields[collection][key] = index_params
            return Column(column_name,index_params['type'],index = field.indexed)

        self._metadata = MetaData()

        for cls in self.classes:
            collection = self.get_collection_for_cls(cls)

            extra_columns = []

            meta_attributes = self.get_meta_attributes(cls)

            for key,field in cls._fields.items():
                if not isinstance(field,BaseField):
                    raise AttributeError("Not a valid field: %s = %s" % (key,field))
                if isinstance(field,ForeignKeyField):
                    extra_columns.append(add_foreign_key_field(collection,key,field))
                elif isinstance(field,ManyToManyField):
                    add_many_to_many_field(collection,key,field)
                elif isinstance(field,ListField):
                    add_list_field(collection,key,field)
                else:
                    extra_columns.append(add_field(collection,key,field))

            self._collection_tables[collection] = Table('%s%s' % (collection,self.table_postfix),self._metadata,
                    Column('pk',self.Meta.PkType,primary_key = True,index = True),
                    Column('data',LargeBinary),
                    *extra_columns
                )

    def get_collection_table(self,collection):
        return self._collection_tables[collection]

    def begin(self):
        if self._transaction:
            self.commit()
        self._transaction = self.connection.begin()

    def commit(self):
        self._transaction.commit()
        self._transaction = None
        self.begin()

    def rollback(self):
        self._transaction.rollback()
        self._transaction = None
        self.begin()

    def close_connection(self):
        return self.connection.close()

    def create_schema(self,indexes = None):
        self.init_schema()
        self._metadata.create_all(self._engine,checkfirst = True)

    def drop_schema(self):
        self.init_schema()
        self._metadata.drop_all(self._engine,checkfirst = True)

    def delete(self, obj):

        if hasattr(obj, 'pre_delete') and callable(obj.pre_delete):
            obj.pre_delete()

        if obj.pk == None:
            raise obj.DoesNotExist
        
        self.filter(obj.__class__,{'pk' : obj.pk}).delete()

    def update(self,obj,set_fields=None, unset_fields=None, update_obj=True):
        if set_fields is not None:
            if isinstance(set_fields,dict):
                for key,value in set_fields.items():
                    obj[key] = value
        if unset_fields is not None:
            for key in unset_fields:
                del obj[key]
        self.save(obj)
        return obj

    def save(self,obj,autosave_dependent = True):
        collection = self.get_collection_for_cls(obj.__class__)
        table = self._collection_tables[collection]

        """
        Document save strategy:

        - Retrieve values for simple embedded index fields
        - Store object data with index fields in DB
        - Retrieve values for list index fields
        - Store each list value in the index table
        - Retrieve related objects
        - Store related objects in the DB
        """

        if hasattr(obj, 'pre_save') and callable(obj.pre_save):
            obj.pre_save()

        def serialize_and_update_indexes(obj,d):
            for index_field,index_params in self._index_fields[collection].items():
                try:
                    value = _get_value(obj,index_field)
                    if isinstance(index_params['field'],ListField):
                        #to do: check if this is a RelatedList
                        table = self._index_tables[collection][index_field]
                        delete = table.delete().where(table.c['pk'] == expression.cast(obj.pk,self.Meta.PkType))
                        self.connection.execute(delete)
                        for element in value:
                            ed = {
                                'pk' : expression.cast(obj.pk,self.Meta.PkType),
                                index_params['column'] : expression.cast(element,index_params['type']),
                            }
                            insert = table.insert().values(**ed)
                            self.connection.execute(insert)
                    else:
                        if value is None:
                            if not index_params['field'].nullable:
                                raise ValueError("No value for %s given, but this is a mandatory field!" % index_field['key'])
                            d[index_params['column']] = null()
                        else:
                            d[index_params['column']] = expression.cast(value,index_params['type'])
                except KeyError:
                    if not isinstance(index_params['field'],ListField):
                        if not index_params['field'].nullable:
                            raise ValueError("No value for %s given, but this is a mandatory field!" % index_field['key'])
                        d[index_params['column']] = null()

        def serialize_and_update_relations(obj,d):
            for related_field,relation_params in self._related_fields[collection].items():
                try:
                    value = _get_value(obj,related_field)
                    if isinstance(relation_params['field'],ManyToManyField):
                        relationship_table = self._relationship_tables[collection][related_field]
                        delete = relationship_table.delete().where(relationship_table.c['pk_%s' % collection] == expression.cast(obj.pk,self.Meta.PkType))
                        self.connection.execute(delete)
                        for element in value:
                            if not isinstance(element,Document):
                                raise AttributeError("ManyToMany field %s contains an invalid value!" % related_field)
                            if element.pk is None:
                                if autosave_dependent:
                                    self.save(element)
                                else:
                                    raise AttributeError("Related document in field %s has no primary key!" % related_field)
                            ed = {
                                'pk_%s' % collection : obj.pk,
                                'pk_%s' % relation_params['collection'] : element.pk,
                            }
                            insert = relationship_table.insert().values(**ed)
                            self.connection.execute(insert)
                    elif isinstance(relation_params['field'],ForeignKeyField):
                        if not isinstance(value,Document):
                            raise AttributeError("Field %s must be a document!" % related_field)
                        if value.pk is None:
                            if autosave_dependent:
                                self.save(value)
                            else:
                                raise AttributeError("Related document in field %s has no primary key!" % related_field)
                        d[relation_params['column']] = expression.cast(value.pk,self.Meta.PkType)

                except KeyError:
                    #this index value does not exist in the object
                    pass

        insert = False
        if not obj.pk:
            obj.pk = uuid.uuid4().hex
            insert = True

        d = {'data' : JsonSerializer.serialize(self.serialize(obj.attributes)),
             'pk' : expression.cast(obj.pk,self.Meta.PkType)}

        serialize_and_update_indexes(obj,d)
        serialize_and_update_relations(obj,d)

        #if we got an object with a PK, we try to perform an UPDATE operation
        if not insert:
            update = self._collection_tables[collection].update().values(**d).where(table.c.pk == obj.pk)
            result = self.connection.execute(update)

        #if we did not get a PK the UPDATE did not match any rows, we perform an INSERT instead
        if insert or not result.rowcount:
            insert = self._collection_tables[collection].insert().values(**d)
            result = self.connection.execute(insert)

        return obj

    def serialize(self, obj, convert_keys_to_str=True, embed_level=0, encoders=None,**kwargs):
        """
        Serialization strategy:
        """
        return super(Backend, self).serialize(obj,
                                              convert_keys_to_str=convert_keys_to_str, 
                                              embed_level=embed_level,
                                              **kwargs)

    def create_instance(self, collection_or_class,attributes, lazy = False):
        data = attributes.get('data',{})
        for key,value in attributes.items():
            if key == 'data':
                continue
            data[key] = value
        if isinstance(collection_or_class,six.string_types):
            collection = collection_or_class
        else:
            collection = self.get_collection_for_cls(collection_or_class)

        #we create the object first
        obj = super(Backend,self).create_instance(collection_or_class, data,lazy)

        #now we update the data dictionary with foreign key fields...
        for field_name,params in self._list_indexes[collection].items():
            _set_value(data,field_name,ListProxy(obj,field_name,params))
        for field_name,params in self._related_fields[collection].items():
            if isinstance(params['field'],ManyToManyField):
                _set_value(data,field_name,ManyToManyProxy(obj,field_name,params))
            elif isinstance(params['field'],ForeignKeyField):
                try:
                    foreign_pk = _get_value(attributes,field_name)
                except KeyError:
                    continue
                if foreign_pk:
                    foreign_obj = self.create_instance(params['class'],{'pk' : foreign_pk},lazy = True)
                else:
                    foreign_obj = None
                _set_value(data,field_name,foreign_obj)
        #we update the attributes of the object
        obj.attributes = data
        return obj

    def deserialize(self, obj, encoders=None):
        return super(Backend, self).deserialize(obj,encoders = encoders)

    def create_index(self, cls_or_collection, *args, **kwargs):
        if not isinstance(cls_or_collection, six.string_types):
            collection = self.get_collection_for_cls(cls_or_collection)
        else:
            collection = cls_or_collection
        self.db[collection].ensure_index(*args, **kwargs)

    def get(self, cls_or_collection, query):

        if not isinstance(cls_or_collection, six.string_types):
            collection = self.get_collection_for_cls(cls_or_collection)
            cls = cls_or_collection
        else:
            collection = cls_or_collection
            cls = self.get_cls_for_collection(collection)

        result = self.filter(cls_or_collection,query)
        try:
            return result[0]
        except IndexError:
            raise cls.DoesNotExist


    def filter(self, cls_or_collection, query, sort_by=None, limit=None, offset=None):
        """
        Filter objects from the database that correspond to a given set of properties.

        See :py:meth:`blitzdb.backends.base.Backend.filter` for documentation of individual parameters

        .. note::

            This function supports all query operators that are available in SQLAlchemy and returns a query set
            that is based on a SQLAlchemy cursor.

        Strategy:

        - Detect all index fields in the query.
        - For each index field, determine the type of the index.
            - If it is a ForeignKey index, use the pk element of the 

        SELECT query generation:

          - Non-indexed fields -> Raise an exception
          - Normal (in-table) index -> Make a query over the indexed field
          - List index -> Make a PK query over the index table
          - ForeignKey relation with related collection:
            - If `related.pk` or `related` used in query, directly query PK value in table
            - If deep field (e.g. `related.name`), make select over PK values of index field
              with result on query on the related table.

        """

        if not isinstance(cls_or_collection, six.string_types):
            collection = self.get_collection_for_cls(cls_or_collection)
            cls = cls_or_collection
        else:
            collection = cls_or_collection
            cls = self.get_cls_for_collection(collection)

        table = self._collection_tables[collection]

        joins = []

        def compile_query(collection,query,table = None):

            """
            This function emits a list of WHERE statements that can be used to retrieve 
            """

            if table is None:
                table = self._collection_tables[collection]

            where_statements  = []

            if len(query) == 1 and query.keys()[0].startswith('$'):
                #this is a special operator query
                operator = query.keys()[0][1:]
                if not operator in ('and','or','not'):
                    raise AttributeError("Non-supported logical operator: $%s" % operator)
                if operator in ('and','or'):
                    where_statements = [sq for expr in query['$%s' % operator] for sq in compile_query(collection,expr)]
                    if operator == 'and':
                        return [and_(*where_statements)]
                    else:
                        return [or_(*where_statements)]
                elif operator  == 'not':
                    return not_(compile_query(query['$not']))

            def prepare_subquery(tail,query_dict):
                d = {}
                if not tail:
                    if isinstance(query_dict,dict):
                        return query_dict.copy()
                    if not isinstance(query_dict,Document):
                        raise AttributeError("Must be a document!")
                    if not query_dict.pk:
                        raise AttributeError("Performing a query without a primary key!")
                    return {'pk' : query_dict.pk}
                if isinstance(query_dict,dict):
                    return {'.'.join([tail,k]) : v for k,v in query_dict.items()}
                else:
                    return {tail : query_dict}

            def prepare_special_query(field_name,query):
                if '$not' in query:
                    return [not_(*prepare_special_query(field_name,query['$not']))]
                elif '$in' in query:
                    return [table.c[field_name].in_(query['$in'])]
                elif '$nin' in query:
                    return [~table.c[field_name].in_(query['$in'])]
                elif '$eq' in query:
                    return [table.c[field_name] == query['$eq']]
                elif '$ne' in query:
                    return [table.c[field_name] != query['$ne']]
                elif '$gt' in query:
                    return [table.c[field_name] > query['$gt']]
                elif '$gte' in query:
                    return [table.c[field_name] >= query['$gte']]
                elif '$lt' in query:
                    return [table.c[field_name] < query['$lt']]
                elif '$lte' in query:
                    return [table.c[field_name] <= query['$lte']]
                elif '$exists' in query:
                    if query['$exists']:
                        return [table.c[field_name] != None]
                    else:
                        return [table.c[field_name] == None]
                elif '$like' in query:
                    where_statements.append(table.c[field_name].like(expression.cast(query['$regex'],String)))
                elif '$regex' in query:
                    if not self._engine.url.drivername in ('postgres','mysql'):
                        raise AttributeError("Regex queries not supported with %s engine!" % self._engine.url.drivername)
                    where_statements.append(table.c[field_name].op('REGEXP')(expression.cast(query['$regex'],String)))
                else:
                    raise AttributeError("Invalid query!")

            #this is a normal, field-base query
            for key,value in query.items():
                if key == 'pk':
                    where_statements.append(table.c.pk == expression.cast(value,self.Meta.PkType))
                    continue
                for field_name,params in self._index_fields[collection].items():
                    if key == field_name:
                        #this is a list-indexed field
                        if isinstance(params['field'],ListField):
                            index_table = self._index_tables[collection][field_name]
                            if isinstance(value,dict):
                                related_query = lambda op: index_table.c[field_name].in_([expression.cast(v,params['type']) for v in value[op]])
                                if '$in' in value:
                                    #do type-cast here?
                                    where_statements.append(related_query('$in'))
                                elif '$nin' in value:
                                    where_statements.append(~related_query('$nin'))
                                elif '$all' in value:
                                    pk_label = 'pk_%s' % field_name
                                    pk_column = index_table.c['pk'].label(pk_label)
                                    cnt = func.count(pk_column).label('cnt')
                                    subselect = select([cnt,pk_column],use_labels = True).where(related_query('$all')).group_by(pk_column)
                                    where_statements.append(table.c.pk.in_(select([subselect.columns[pk_label]]).where(subselect.columns['cnt'] == len(value['$all']))))
                                elif '$size' in value:
                                    raise NotImplementedError("$size operator is not yet implemented!")
                                else:
                                    raise AttributeError("Invalid query!")
                            else:
                                where_statements.append(index_table.c[field_name] == expression.cast(value,params['type']))
                        else:
                            #this is a normal column index
                            if isinstance(value,dict):
                                #this is a special query
                                where_statements.extend(prepare_special_query(field_name,value))
                            else:
                                #this is a normal value query
                                where_statements.append(table.c[field_name] == expression.cast(value,params['type']))
                        break
                else:
                    #this is a non-indexed field! We try to find a relation...
                    for field_name,params in self._related_fields[collection].items():
                        if key.startswith(field_name):
                            if isinstance(params['field'],ManyToManyField):
                                relationship_table = self._relationship_tables[collection][field_name]
                                related_collection = params['collection']
                                related_table = self._collection_tables[related_collection]
                                tail = key[len(field_name)+1:]
                                #this is a query for a document
                                if isinstance(value,Document) and not tail:
                                    value = {'pk' : value.pk}
                                elif not isinstance(value,dict):
                                    if tail:
                                        value = {tail : value}
                                    else:
                                        raise AttributeError("Query over a ManyToMany field must be a dictionary!")
                                #to do: allow modifiers when using special queries (e.g. for regex)
                                #Currently we only support $elemMatch, $all and $in operators
                                if len(value) == 1 and value.keys()[0].startswith('$'):
                                    operator = value.keys()[0][1:]
                                    subquery = value.values()[0]
                                    if operator == 'elemMatch':
                                        query_type = 'all'
                                        queries = compile_query(params['collection'],prepare_subquery(tail,value['$elemMatch']))
                                    elif operator == 'all':
                                        query_type = 'all'
                                        if len(subquery) and isinstance(subquery[0],dict) and len(subquery[0]) == 1 and \
                                        subquery[0].keys()[0] == '$elemMatch':
                                            queries = [sq for v in subquery for sq in compile_query(params['collection'],prepare_subquery(tail,v['$elemMatch']))]
                                        else:
                                            queries = [sq for v in subquery for sq in compile_query(params['collection'],prepare_subquery(tail,v))]
                                    elif operator == 'in':
                                        query_type = 'in'
                                        queries = [sq for v in subquery for sq in compile_query(params['collection'],prepare_subquery(tail,v))]
                                    elif operator == 'nin':
                                        query_type = 'nin'
                                        queries = [sq for v in subquery for sq in compile_query(params['collection'],prepare_subquery(tail,v))]
                                    elif operator == '$size':
                                        raise AttributeError("Size operator is currently not supported!")
                                    else:
                                        raise AttributeError("Unsupported operator: %s" % operator)
                                else:
                                    query_type = 'all'
                                    queries = compile_query(params['collection'],value)
                                    #this is an exact query
                                related_select = select([related_table.c.pk]).where(or_(*queries))
                                related_query = relationship_table.c['pk_%s' % related_collection].in_(related_select)
                                pk_column = relationship_table.c['pk_%s' % collection].label('pk')
                                if query_type == 'all':
                                    cnt = func.count(pk_column).label('cnt')
                                    s = select([pk_column]).where(related_query).group_by('pk').having(cnt == len(queries))
                                elif query_type == 'in':
                                    s = select([pk_column]).where(related_query)
                                elif query_type == 'nin':
                                    s = select([pk_column]).where(not_(related_query))
                                else:
                                    raise AttributeError("Invalid query!")
                                where_statements.append(table.c.pk.in_(s))
                            else:#this is a normal ForeignKey relation
                                if key == field_name:
                                    if not isinstance(value,Document):
                                        raise AttributeError("ForeignKey query with non-document!")
                                    where_statements.append(table.c[params['column']] == value.pk)
                                else:
                                    #we query a sub-field of the relation
                                    head,tail = key[:len(field_name)],key[len(field_name)+1:]
                                    related_table = self._collection_tables[params['collection']]
                                    if not related_table in joins:
                                        related_table_alias = related_table.alias()
                                        joins.append((related_table_alias,table.c[params['column']] == related_table_alias.c['pk']))
                                    where_statements.extend(compile_query(params['collection'],{tail : value},table = related_table_alias))
                            break
                    else:
                        raise AttributeError("Query over non-indexed field %s in collection %s!" % (key,collection))
            return where_statements

        compiled_query = compile_query(collection,query)

        if len(compiled_query) > 1:
            compiled_query = and_(*compiled_query)
        elif compiled_query:
            compiled_query = compiled_query[0]
        else:
            compiled_query = None

        return QuerySet(backend = self, table = table,joins = joins,cls = cls,condition = compiled_query)
