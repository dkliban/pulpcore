from collections import OrderedDict
import re

from drf_yasg.generators import OpenAPISchemaGenerator
from drf_yasg.inspectors import SwaggerAutoSchema
from drf_yasg import openapi
from drf_yasg.utils import filter_none, force_real_str
import uritemplate


class Paths(openapi.SwaggerDict):
    def __init__(self, paths, **extra):
        """A listing of all the paths in the API.

        :param dict[str,.PathItem] paths:
        """
        super(Paths, self).__init__(**extra)
        for path, path_obj in paths.items():
            if path_obj is not None:  # pragma: no cover
                self[path] = path_obj
        self._insert_extras__()


class PulpOpenAPISchemaGenerator(OpenAPISchemaGenerator):

    def __init__(self, info, version='', url=None, patterns=None, urlconf=None):
        """
        Args:
            info (drf_yasg.openapi.Info): information about the API
            version (str): API version string; if omitted, `info.default_version` will be used
            url (str): API scheme, host and port; if ``None`` is passed and ``DEFAULT_API_URL`` is
                not set, the url will be inferred from the request made against the schema view, so
                you should generally not need to set this parameter explicitly; if the empty string
                is passed, no host and scheme will be emitted If `url` is not ``None`` or the empty
                string, it must be a scheme-absolute uri (i.e. starting with http:// or https://),
                and any path component is ignored.
            patterns: if given, only these patterns will be enumerated for inclusion in the API spec
            urlconf: if patterns is not given, use this urlconf to enumerate patterns;
                if not given, the default urlconf is used
        """
        self.tags = []
        super().__init__(info, version=version, url=url, patterns=patterns, urlconf=urlconf)

    def get_paths(self, endpoints, components, request, public):
        """Generate the Swagger Paths for the API from the given endpoints.

        Args:
            endpoints (dict): endpoints as returned by get_endpoints
            components (ReferenceResolver): resolver/container for Swagger References
            request (Request): the request made against the schema view; can be None
            public (bool): if True, all endpoints are included regardless of access through
                           `request`

        Returns:
            tuple[openapi.Paths,str]: The :class:`openapi.Paths` object and the longest common path
                                      prefix, as a 2-tuple
        """
        if not endpoints:
            return openapi.Paths(paths={}), ''

        plugin_filter = None
        if 'plugin' in request.GET:
            plugin_filter = request.GET['plugin']
        prefix = ''
        resources = {}
        resource_example = {}
        paths = OrderedDict()
        for path, (view_cls, methods) in sorted(endpoints.items()):
            operations = {}
            for method, view in methods:
                if plugin_filter:
                    if view.__module__.split('.')[0] != plugin_filter:
                        continue
                if not public and not self._gen.has_view_permissions(path, method, view):
                    continue

                operation = self.get_operation(view, path, prefix, method, components, request)
                if operation is not None:
                    operations[method.lower()] = operation
                    tag = operation.tags[0]
                    tag_dict = {"name": tag, "x-displayName": tag.title()}
                    tag_exists = False
                    for tag in self.tags:
                        if tag["name"] == tag_dict["name"]:
                            tag_exists = True
                    if not tag_exists:
                        self.tags.append(tag_dict)

            if operations:
                path_param = None

                if '}' in path:
                    resource_path = '%s}/' % path.rsplit(sep='}', maxsplit=1)[0]
                    resource_other_path = self.get_resource_from_path(path)
                    if resource_other_path in endpoints:
                        resource_model = endpoints[resource_other_path][0].queryset.model
                        resource_name = self.get_parameter_name(resource_model)
                        param_name = self.get_parameter_slug_from_model(resource_model)
                        if resource_path in resources:
                            path = path.replace(resource_path, '{%s}' % resources[resource_path])
                        elif resource_other_path in resources:
                            path = path.replace(resource_path, '{%s}' % resources[
                                resource_other_path])
                        else:
                            resources[resource_path] = param_name
                            resource_example[resource_path] = self.get_example_uri(path)
                            path = path.replace(resource_path, '{%s}' % resources[resource_path])
                        example = resource_example[resource_other_path]
                        resource_description = self.get_resource_description(resource_name, example)
                        path_param = openapi.Parameter(
                            name=param_name,
                            description=resource_description,
                            required=True,
                            in_=openapi.IN_PATH,
                            type=openapi.TYPE_STRING,
                        )
                        paths[path] = openapi.PathItem(parameters=[path_param], **operations)
                    else:
                        if not path.startswith('/'):
                            path = '/' + path
                        paths[path] = self.get_path_item(path, view_cls, operations)
                else:
                    paths[path] = openapi.PathItem(parameters=[path_param], **operations)

        return Paths(paths=paths), prefix

    @staticmethod
    def get_resource_from_path(path):
        """
        Returns a path for a resource nested in the specified path

        Args:
            path (str): Full path to be searched for a nested resource

        Returns:
            str: path of nested resource
        """
        resource_path = '%s}/' % path.rsplit(sep='}', maxsplit=1)[0]
        if resource_path.endswith('_pk}/'):
            resource_path = '%s{_id}/' % resource_path.rsplit(sep='{', maxsplit=1)[0]
        return resource_path

    @staticmethod
    def get_resource_description(name, example_uri):
        """Returns a description of an *_href path parameter

        Args:
            name (str): Name of the resource referenced by the *_href path parameter
            example_uri (str): An example of the URI that is a reference for a specific resource

        Returns:
            str: Description of an *_href path parameter
        """
        return "URI of %s. e.g.: %s" % (name, example_uri)

    @staticmethod
    def get_example_uri(path):
        """Returns an example URI for a path template

        Args:
            path (openapi.Path): path object for a specific resource


        Returns:
            str: The path with concrete path parameters.
        """
        params = {}
        for variable in uritemplate.variables(path):
            params[variable] = '1'
        return uritemplate.expand(path, **params)

    @staticmethod
    def get_parameter_slug_from_model(model):
        """Returns a path parameter name for the resource associated with the model.

        Args:
            model (django.db.models.Model): The model for which a path parameter name is needed

        Returns:
            str: *_href where * is the model name in all lower case letters
        """
        return '%s_href' % '_'.join([part.lower() for part in re.findall('[A-Z][^A-Z]*',
                                                                         model.__name__)])

    @staticmethod
    def get_parameter_name(model):
        """Returns the human readable name of the resource associated with the model

        Args:
            model (django.db.models.Model): The model for which a name is needed

        Returns:
            str: name of the resource associated with the model
        """
        return ' '.join(re.findall('[A-Z][^A-Z]*', model.__name__))

    def get_operation_keys(self, subpath, method, view):
        """Return a list of keys that should be used to group an operation within the specification. ::

          /users/                   ("users", "list"), ("users", "create")
          /users/{pk}/              ("users", "read"), ("users", "update"), ("users", "delete")
          /users/enabled/           ("users", "enabled")  # custom viewset list action
          /users/{pk}/star/         ("users", "star")     # custom viewset detail action
          /users/{pk}/groups/       ("users", "groups", "list"), ("users", "groups", "create")
          /users/{pk}/groups/{pk}/  ("users", "groups", "read"), ("users", "groups", "update")

        The path prefix, /pulp/api/v3/, is ignored.

        Args:
            subpath (str): path to the operation with any common prefix/base path removed
            method (str): HTTP method
            view (rest_framework.views.APIView): the view associated with the operation

        Returns:
            List of strings
        """
        subpath = subpath.replace('/pulp/api/v3', '')
        return super().get_operation_keys(subpath, method, view)

    def get_schema(self, request=None, public=False):
        """Generate a :class:`.Swagger` object representing the API schema.

        This method also adds tags to the schema definition. This allows ReDoc to provide a display
        name for each section of the docs.

        Args:
            request (rest_framework.request.Request): the request used for filtering accessible
                endpoints and finding the spec URI. Can be None.
            public (bool): if True, all endpoints are included regardless of access through
                `request`

        Returns:
            openapi.Swagger: The generated Swagger specification
        """
        schema = super().get_schema(request=request, public=public)
        schema.tags = self.tags
        return schema


class PulpAutoSchema(SwaggerAutoSchema):
    """
    Auto schema inspector for Pulp. This inspector is able to generate nice desriptions for all
    operations.
    """

    def get_operation(self, operation_keys):
        consumes = self.get_consumes()
        produces = self.get_produces()

        body = self.get_request_body_parameters(consumes)
        query = self.get_query_parameters()
        parameters = body + query
        parameters = filter_none(parameters)
        parameters = self.add_manual_parameters(parameters)

        operation_id = self.get_operation_id(operation_keys)
        summary, description = self.get_summary_and_description()
        security = self.get_security()
        assert security is None or isinstance(security, list), "security must be a list of " \
                                                               "security requirement objects"
        deprecated = self.is_deprecated()
        tags = self.get_tags(operation_keys)

        responses = self.get_responses()
        if 'operation_summary' not in self.overrides:
            summary = self.get_summary(operation_keys)
        return openapi.Operation(
            operation_id=operation_id,
            description=force_real_str(description),
            summary=force_real_str(summary),
            responses=responses,
            parameters=parameters,
            consumes=consumes,
            produces=produces,
            tags=tags,
            security=security,
            deprecated=deprecated
        )

    def get_summary(self, operation_keys):
        """
        Returns summary of operation.

        This is the value that is displayed in the ReDoc document as the short name for the API
        operation.
        """
        if not hasattr(self.view, 'queryset') or self.view.queryset is None:
            return self.get_summary_and_description()[0]
        model = self.view.queryset.model
        operation = operation_keys[-1]
        resource = model._meta.verbose_name
        article = 'a'
        if resource[0].lower() in 'aeiou':
            article = 'an'
        if operation == 'read':
            return f'Inspect {article} {resource}'
        elif operation == 'list':
            resource = model._meta.verbose_name_plural
            return f'List {resource}'
        elif operation == 'create':
            return f'Create {article} {resource}'
        elif operation == 'update':
            return f'Update {article} {resource}'
        elif operation == 'delete':
            return f'Delete {article} {resource}'
        elif operation == 'partial_update':
            return f'Partially update {article} {resource}'
