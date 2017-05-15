# -*- coding: utf-8 -*-

# TODO: datetime support

###
### DO NOT CHANGE THIS FILE
### 
### The code is auto generated, your change will be overwritten by 
### code generating.
###


DefinitionsContent = {'minLength': 2, 'type': 'string', 'maxLength': 1024}
DefinitionsCreatequestion = {'required': ['content'], 'properties': {'content': DefinitionsContent, 'is_public': {'type': 'boolean'}, 'images': {'items': {'type': 'string'}, 'type': 'array', 'maxItems': 3}, 'form_id': {'type': 'string'}}}

validators = {
    ('game_roles', 'GET'): {'headers': {'required': [], 'properties': {'Authorization': {'required': False, 'type': 'string'}}}, 'args': {'required': [], 'properties': {'per_page': {'description': 'per_page number', 'format': 'int32', 'required': False, 'type': 'integer', 'maximum': 100}, 'limit': {'description': 'limit number', 'format': 'int32', 'required': False, 'type': 'integer', 'maximum': 100}, 'page': {'description': 'page number', 'format': 'int32', 'required': False, 'type': 'integer', 'maximum': 10000}, 'offset': {'description': 'offset number', 'format': 'int32', 'required': False, 'type': 'integer'}}}},
    ('game_roles', 'POST'): {'headers': {'required': [], 'properties': {'Authorization': {'type': 'string'}}}, 'json': DefinitionsCreatequestion},
}

filters = {
    ('game_roles', 'GET'): {200: {'headers': None, 'schema': {'items': DefinitionsContent, 'type': 'array'}}},
    ('game_roles', 'POST'): {201: {'headers': None, 'schema': DefinitionsContent}},
}

scopes = {
    ('game_roles', 'GET'): ['open'],
    ('game_roles', 'POST'): ['open'],
}


def merge_default(schema, value, get_first=True):
    # TODO: more types support
    type_defaults = {
        'integer': 9573,
        'string': 'something',
        'object': {},
        'array': [],
        'boolean': False
    }

    results = normalize(schema, value, type_defaults)
    if get_first:
        return results[0]
    return results


def normalize(schema, data, required_defaults=None):

    import six

    if required_defaults is None:
        required_defaults = {}
    errors = []

    class DataWrapper(object):

        def __init__(self, data):
            super(DataWrapper, self).__init__()
            self.data = data

        def get(self, key, default=None):
            if isinstance(self.data, dict):
                return self.data.get(key, default)
            return getattr(self.data, key, default)

        def has(self, key):
            if isinstance(self.data, dict):
                return key in self.data
            return hasattr(self.data, key)

        def keys(self):
            if isinstance(self.data, dict):
                return list(self.data.keys())
            return list(vars(self.data).keys())

        def get_check(self, key, default=None):
            if isinstance(self.data, dict):
                value = self.data.get(key, default)
                has_key = key in self.data
            else:
                try:
                    value = getattr(self.data, key)
                except AttributeError:
                    value = default
                    has_key = False
                else:
                    has_key = True
            return value, has_key

    def _normalize_dict(schema, data):
        result = {}
        if not isinstance(data, DataWrapper):
            data = DataWrapper(data)

        for key, _schema in six.iteritems(schema.get('properties', {})):
            # set default
            type_ = _schema.get('type', 'object')

            # get value
            value, has_key = data.get_check(key)
            if has_key:
                result[key] = _normalize(_schema, value)
            elif 'default' in _schema:
                result[key] = _schema['default']
            elif key in schema.get('required', []):
                if type_ in required_defaults:
                    result[key] = required_defaults[type_]
                else:
                    errors.append(dict(name='property_missing',
                                       message='`%s` is required' % key))

        for _schema in schema.get('allOf', []):
            rs_component = _normalize(_schema, data)
            rs_component.update(result)
            result = rs_component

        additional_properties_schema = schema.get('additionalProperties', False)
        if additional_properties_schema:
            aproperties_set = set(data.keys()) - set(result.keys())
            for pro in aproperties_set:
                result[pro] = _normalize(additional_properties_schema, data.get(pro))

        return result

    def _normalize_list(schema, data):
        result = []
        if hasattr(data, '__iter__') and not isinstance(data, dict):
            for item in data:
                result.append(_normalize(schema.get('items'), item))
        elif 'default' in schema:
            result = schema['default']
        return result

    def _normalize_default(schema, data):
        if data is None:
            return schema.get('default')
        else:
            return data

    def _normalize(schema, data):
        if not schema:
            return None
        funcs = {
            'object': _normalize_dict,
            'array': _normalize_list,
            'default': _normalize_default,
        }
        type_ = schema.get('type', 'object')
        if not type_ in funcs:
            type_ = 'default'

        return funcs[type_](schema, data)

    return _normalize(schema, data), errors

