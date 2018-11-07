"""
sentry.db.models.fields.node
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:copyright: (c) 2010-2014 by the Sentry Team, see AUTHORS for more details.
:license: BSD, see LICENSE for more details.
"""

from __future__ import absolute_import, print_function

import collections
import logging
import six
import warnings

from django.conf import settings
from django.db import models
from django.db.models.signals import post_delete

from sentry import nodestore
from sentry.utils.compat import pickle
from sentry.utils.strings import decompress, compress
from sentry.utils.canonical import CANONICAL_TYPES, CanonicalKeyDict

from .gzippeddict import GzippedDictField

__all__ = ('NodeField', )

logger = logging.getLogger('sentry')


class NodeUnpopulated(Exception):
    pass


class NodeIntegrityFailure(Exception):
    pass


class NodeData(collections.MutableMapping):
    def __init__(self, field, id, data=None):
        self.field = field
        self.id = id
        self.ref = None
        # ref version is used to discredit a previous ref
        # (this does not mean the Event is mutable, it just removes ref checking
        #  in the case of something changing on the data model)
        self.ref_version = None
        self._node_data = data

    def __getstate__(self):
        data = dict(self.__dict__)
        # downgrade this into a normal dict in case it's a shim dict.
        # This is needed as older workers might not know about newer
        # collection types.  For isntance we have events where this is a
        # CanonicalKeyDict
        data.pop('data', None)
        data['_node_data_CANONICAL'] = isinstance(data['_node_data'], CANONICAL_TYPES)
        data['_node_data'] = dict(data['_node_data'].items())
        return data

    def __setstate__(self, state):
        # If there is a legacy pickled version that used to have data as a
        # duplicate, reject it.
        state.pop('data', None)
        if state.pop('_node_data_CANONICAL', False):
            state['_node_data'] = CanonicalKeyDict(state['_node_data'])
        self.__dict__ = state

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value

    def __delitem__(self, key):
        del self.data[key]

    def __iter__(self):
        return iter(self.data)

    def __len__(self):
        return len(self.data)

    def __repr__(self):
        cls_name = type(self).__name__
        if self._node_data:
            return '<%s: id=%s data=%r>' % (cls_name, self.id, repr(self._node_data))
        return '<%s: id=%s>' % (cls_name, self.id, )

    def get_ref(self, instance):
        ref_func = self.field.ref_func
        if not ref_func:
            return
        return ref_func(instance)

    def copy(self):
        return self.data.copy()

    @property
    def data(self):
        if self._node_data is not None:
            return self._node_data

        elif self.id:
            if settings.DEBUG:
                raise NodeUnpopulated('You should populate node data before accessing it.')
            else:
                warnings.warn('You should populate node data before accessing it.')
            self.bind_data(nodestore.get(self.id) or {})
            return self._node_data

        rv = {}
        if self.field.wrapper is not None:
            rv = self.field.wrapper(rv)
        return rv

    def bind_data(self, data, ref=None):
        self.ref = data.pop('_ref', ref)
        self.ref_version = data.pop('_ref_version', None)
        if self.ref_version == self.field.ref_version and ref is not None and self.ref != ref:
            raise NodeIntegrityFailure(
                'Node reference for %s is invalid: %s != %s' % (self.id, ref, self.ref, )
            )
        if self.field.wrapper is not None:
            data = self.field.wrapper(data)
        self._node_data = data

    def bind_ref(self, instance):
        ref = self.get_ref(instance)
        if ref:
            self.data['_ref'] = ref
            self.data['_ref_version'] = self.field.ref_version


class NodeField(GzippedDictField):
    """
    Similar to the gzippedictfield except that it stores a reference
    to an external node.
    """

    def __init__(self, *args, **kwargs):
        self.ref_func = kwargs.pop('ref_func', None)
        self.ref_version = kwargs.pop('ref_version', None)
        self.wrapper = kwargs.pop('wrapper', None)
        super(NodeField, self).__init__(*args, **kwargs)

    def contribute_to_class(self, cls, name):
        super(NodeField, self).contribute_to_class(cls, name)
        post_delete.connect(self.on_delete, sender=self.model, weak=False)

    def on_delete(self, instance, **kwargs):
        value = getattr(instance, self.name)
        if not value.id:
            return

        nodestore.delete(value.id)

    def to_python(self, value):
        if isinstance(value, six.string_types) and value:
            try:
                value = pickle.loads(decompress(value))
            except Exception as e:
                logger.exception(e)
                value = {}
        elif not value:
            value = {}

        if 'node_id' in value:
            node_id = value.pop('node_id')
            data = None
        else:
            node_id = None
            data = value

        if self.wrapper is not None and data is not None:
            data = self.wrapper(data)

        return NodeData(self, node_id, data)

    def get_prep_value(self, value):
        if not value and self.null:
            # save ourselves some storage
            return None

        # We can't put our wrappers into the nodestore, so we need to
        # ensure that the data is converted into a plain old dict
        data = value.data
        if isinstance(data, CANONICAL_TYPES):
            data = dict(data.items())

        # TODO(dcramer): we should probably do this more intelligently
        # and manually
        if not value.id:
            value.id = nodestore.create(data)
        else:
            nodestore.set(value.id, data)

        return compress(pickle.dumps({'node_id': value.id}))


if hasattr(models, 'SubfieldBase'):
    NodeField = six.add_metaclass(models.SubfieldBase)(NodeField)

if 'south' in settings.INSTALLED_APPS:
    from south.modelsinspector import add_introspection_rules

    add_introspection_rules([], ["^sentry\.db\.models\.fields\.node\.NodeField"])
