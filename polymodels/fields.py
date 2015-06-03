from __future__ import unicode_literals

from functools import partial
from inspect import isclass

import django
from django import forms
from django.contrib.contenttypes.models import ContentType
from django.db.models import ForeignKey, Q
from django.db.models.fields import NOT_PROVIDED
from django.db.models.fields.related import RelatedField, ManyToOneRel
from django.utils.translation import ugettext_lazy as _

from .compat import (
    get_content_type, get_remote_field, get_remote_model, lazy_related_operation, LazyObject, string_types
)
from .models import BasePolymorphicModel


class PolymorphicManyToOneRel(ManyToOneRel):
    """
    Relationship that generates a `limit_choices_to` based on it's polymorphic
    type subclasses.
    """

    @property
    def limit_choices_to(self):
        subclasses_lookup = self.polymorphic_type.subclasses_lookup('pk')
        limit_choices_to = self._limit_choices_to
        if limit_choices_to is None:
            limit_choices_to = subclasses_lookup
        elif isinstance(limit_choices_to, dict):
            limit_choices_to = dict(limit_choices_to, **subclasses_lookup)
        elif isinstance(limit_choices_to, Q):
            limit_choices_to = limit_choices_to & Q(**subclasses_lookup)
        # Cache it
        self.__dict__['limit_choices_to'] = limit_choices_to
        return limit_choices_to

    @limit_choices_to.setter
    def limit_choices_to(self, value):
        self._limit_choices_to = value
        # Removed the cached value and return it
        return self.__dict__.pop('limit_choices_to', None)


class LazyPolymorphicTypeQueryset(LazyObject):
    def __init__(self, remote_field, db):
        super(LazyPolymorphicTypeQueryset, self).__init__()
        self.__dict__.update(remote_field=remote_field, db=db)

    def _setup(self):
        remote_field = self.__dict__.get('remote_field')
        db = self.__dict__.get('db')
        self._wrapped = get_remote_model(remote_field)._default_manager.using(db).complex_filter(
            remote_field.limit_choices_to
        )


class PolymorphicTypeField(ForeignKey):
    default_error_messages = {
        'invalid': _('Specified model is not a subclass of %(model)s.')
    }
    description = _(
        'Content type of a subclass of %(type)s'
    )
    rel_class = PolymorphicManyToOneRel

    def __init__(self, polymorphic_type, *args, **kwargs):
        if not isinstance(polymorphic_type, string_types):
            self.validate_polymorphic_type(polymorphic_type)
        defaults = {
            'to': ContentType,
            'related_name': '+',
        }
        # TODO: Remove when support for Django 1.8 is dropped.
        if not hasattr(ForeignKey, 'rel_class'):
            defaults['rel_class'] = PolymorphicManyToOneRel
        defaults.update(kwargs)
        super(PolymorphicTypeField, self).__init__(*args, **defaults)
        get_remote_field(self).polymorphic_type = polymorphic_type

    def validate_polymorphic_type(self, model):
        if not isclass(model) or not issubclass(model, BasePolymorphicModel):
            raise AssertionError(
                "First parameter to `PolymorphicTypeField` must be "
                "a subclass of `BasePolymorphicModel`"
            )

    def contribute_to_class(self, cls, name):
        super(PolymorphicTypeField, self).contribute_to_class(cls, name)
        polymorphic_type = get_remote_field(self).polymorphic_type
        if (isinstance(polymorphic_type, string_types) or
                polymorphic_type._meta.pk is None):
            def resolve_polymorphic_type(model, related_model, field):
                field.validate_polymorphic_type(related_model)
                get_remote_field(field).polymorphic_type = related_model
                field.do_polymorphic_type(related_model)
            lazy_related_operation(resolve_polymorphic_type, cls, polymorphic_type, field=self)
        else:
            self.do_polymorphic_type(polymorphic_type)

    def do_polymorphic_type(self, polymorphic_type):
        if self.default is NOT_PROVIDED and not self.null:
            self.default = partial(get_content_type, polymorphic_type)
        self.type = polymorphic_type.__name__
        self.error_messages['invalid'] = (
            'Specified content type is not of a subclass of %s.' %
            polymorphic_type._meta.object_name
        )

    def formfield(self, **kwargs):
        db = kwargs.pop('using', None)
        remote_model = get_remote_model(get_remote_field(self))
        if isinstance(remote_model, string_types):
            raise ValueError("Cannot create form field for %r yet, because "
                             "its related model %r has not been loaded yet" %
                             (self.name, remote_model))
        defaults = {
            'form_class': forms.ModelChoiceField,
            'queryset': LazyPolymorphicTypeQueryset(get_remote_field(self), db),
            'to_field_name': get_remote_field(self).field_name,
        }
        defaults.update(kwargs)
        return super(RelatedField, self).formfield(**defaults)

    def south_field_triple(self):
        """Provide a suitable description of this field for South."""
        from south.modelsinspector import introspector
        args, kwargs = introspector(self)
        return 'django.db.models.fields.related.ForeignKey', args, kwargs

    if django.VERSION >= (1, 7):
        def deconstruct(self):
            name, _, args, kwargs = super(PolymorphicTypeField, self).deconstruct()
            path = 'django.db.models.fields.related.ForeignKey'
            return name, path, args, kwargs
