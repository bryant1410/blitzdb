import time
import copy
import sqlalchemy

from blitzdb.queryset import QuerySet as BaseQuerySet
from functools import wraps
from sqlalchemy.sql import select,func,expression,delete
from sqlalchemy.sql.expression import join,asc,desc
from ..file.serializers import JsonSerializer

class QuerySet(BaseQuerySet):

    def __init__(self, backend, table, cls,
                 condition = None,select = None,intersects = None,raw = False,joins = None,
                 limit = None,
                 offset = None
                 ):
        super(QuerySet,self).__init__(backend = backend,cls = cls)

        self.joins = joins
        self.backend = backend
        self.condition = condition
        self.select = select
        self.cls = cls
        self._limit = limit
        self._offset = offset
        self.table = table
        self._it = None
        self._raw = raw
        self.count = None
        self.order_bys = None
        self.result = None
        self.intersects = intersects
        self.objects = None
        self.pop_objects = None

    def limit(self,limit):
        self._limit = limit

    def offset(self,offset):
        self._offset = offset

    def deserialize(self, data):
        d = {key : value for key,value in data.items()}
        if 'data' in d:
            d['data'] = JsonSerializer.deserialize(d['data'])
        if self._raw:
            return d
        deserialized_attributes = self.backend.deserialize(d)
        return self.backend.create_instance(self.cls, deserialized_attributes)

    def sort(self, columns):
        order_bys = []
        for column,direction in columns:
            if direction > 0:
                direction = asc
            else:
                direction = desc
            order_bys.append(direction(column))
        self.order_bys = order_bys
        self.objects = None
        return self

    def next(self):
        if self._it is None:
            self._it = iter(self)
        return self._it.next()

    __next__ = next

    def __iter__(self):
        if self.objects is None:
            self.get_objects()
        for obj in self.objects:
            yield self.deserialize(obj)
        raise StopIteration

    def __contains__(self, obj):
        pks = self.distinct_pks()
        if isinstance(obj, list) or isinstance(obj, tuple):
            obj_list = obj
        else:
            obj_list = [obj]
        for obj in obj_list:
            if obj.pk not in pks:
                return False
        return True

    def get_objects(self):
        s = self.get_select()
        try:
            self.objects = self.backend.connection.execute(s).fetchall()
        except sqlalchemy.exc.ResourceClosedError:
            self.objects = None
            raise
        self.pop_objects = self.objects[:]

    def as_list(self):
        if self.objects is None:
            self.get_objects()
        return [self.deserialize(obj) for obj in self.objects]

    def __getitem__(self,key):
        if isinstance(key, slice):
            start, stop, step = key.start, key.stop, key.step
            if step != None:
                raise IndexError("SQL backend dos not support steps in slices")
            if key.start == None:
                start = 0
            if key.stop == None:
                stop = len(self)
            if start < 0:
                start = len(self) + start
            if stop < 0:
                stop = len(self) + stop
            qs = copy.copy(self)
            if start:
                qs.offset(start)
            qs.limit(stop-start)
            qs.objects = None
            return qs
        if self.objects is None:
            self.get_objects()
        return self.deserialize(self.objects[key])

    def pop(self,i = 0):
        if self.objects is None:
            self.get_objects()
        if self.pop_objects:
            return self.deserialize(self.pop_objects.pop())
        raise IndexError("No more results!")

    def filter(self,*args,**kwargs):
        qs = self.backend.filter(*args,**kwargs)
        return self.intersect(qs)

    def delete(self):
        if self.condition is not None:
            delete_stmt = self.table.delete().where(self.condition)
        elif self.select is not None:
            delete_stmt = self.table.delete().where(self.table.c.pk.in_(self.select.select_from([self.table.c.pk])))
        else:
            delete_stmt = self.table.delete()
        self.backend.connection.execute(delete_stmt)

    def get_select(self,fields = None):
        if fields is None:
            fields = [self.table]
        if self.condition is not None:
            s = select(fields).where(self.condition)
        else:
            s = select(fields)
        if self.joins:
            full_join = None
            for j in self.joins:
                if full_join is not None:
                    full_join = full_join.join(*j)
                else:
                    full_join = join(self.table,*j)
            s = s.select_from(full_join)
        if self.order_bys:
            s = s.order_by(*self.order_bys)
        if self._offset:
            s = s.offset(self._offset)
        if self._limit:
            s = s.limit(self._limit)
        return s

    def __len__(self):
        if self.count is None:
            s = self.get_select(fields = [func.count(self.table.c.pk)])
            result = self.backend.connection.execute(s)
            self.count = result.first()[0]
            result.close()
        return self.count

    def distinct_pks(self):
        s = self.get_select([self.table.c.pk]).distinct(self.table.c.pk)
        result = self.backend.connection.execute(s)
        return set([r[0] for r in result.fetchall()])
        
    def __ne__(self, other):
        return not self.__eq__(other)
    
    def __eq__(self, other):
        if isinstance(other, QuerySet): 
            if self.cls == other.cls and len(self) == len(other) \
              and self.distinct_pks() == other.distinct_pks():
                return True
        elif isinstance(other, list):
            if len(other) != len(self.keys):
                return False
            objs = list(self)
            if other == objs:
                return True
        return False

