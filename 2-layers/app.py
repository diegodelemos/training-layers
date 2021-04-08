"""Full architecture example

Run:

    flask run

Create a todo item as user 1:

    curl -X POST \
        -H "Accept: application/json" \
        -H "Content-Type: application/json" \
        -d '{"id": 1, "title": "Test", "priority": 1}' \
        "http://127.0.0.1:5000/todos?user=1"

Get the item again:

    curl -X GET \
        -H "Accept: application/json" \
        "http://127.0.0.1:5000/todos/1?user=1"


Exercise
========

Implement the following REST API:

    curl -X GET \
        -H "Accept: application/json" \
        "http://127.0.0.1:5000/todos?user=1"

The API should list all todo items for a given user. You'll need to make
changes to both the data, service and presentation layer.

Testing the solution:

1. Create some todos

    curl -X POST \
        -H "Accept: application/json" \
        -H "Content-Type: application/json" \
        -d '{"id": 1, "title": "Morning run", "priority": 1}' \
        "http://127.0.0.1:5000/todos?user=1"

    curl -X POST \
        -H "Accept: application/json" \
        -H "Content-Type: application/json" \
        -d '{"id": 2, "title": "Lunch time run", "priority": 1}' \
        "http://127.0.0.1:5000/todos?user=1"

    curl -X POST \
        -H "Accept: application/json" \
        -H "Content-Type: application/json" \
        -d '{"id": 3, "title": "Buy beer", "priority": 1}' \
        "http://127.0.0.1:5000/todos?user=2"

    curl -X POST \
        -H "Accept: application/json" \
        -H "Content-Type: application/json" \
        -d '{"id": 4, "title": "Buy snacks", "priority": 1}' \
        "http://127.0.0.1:5000/todos?user=2"


2. Test that the new endpoint lists only the todos owned by the user:

    $ curl -sX GET \
        -H "Accept: application/json" \
        "http://127.0.0.1:5000/todos?user=1" | jq ".[].title"

    "Morning run"
    "Lunch time run"

    $ curl -sX GET \
        -H "Accept: application/json" \
        "http://127.0.0.1:5000/todos?user=2" | jq ".[].title"

    "Buy beer"
    "Buy snacks"

Note: clearly user 1 is more sportive than user 2. However, user 2 has more friends.
"""

import marshmallow as ma
from flask import Flask
from flask_principal import (AnonymousIdentity, Identity, Permission,
                             PermissionDenied, RoleNeed, UserNeed)
from flask_resources import (HTTPJSONException, JSONDeserializer,
                             JSONSerializer, RequestBodyParser, Resource,
                             ResourceConfig, ResponseHandler,
                             create_error_handler, request_body_parser,
                             request_parser, resource_requestctx,
                             response_handler, route)
from invenio_records_permissions.generators import AuthenticatedUser, Generator
from invenio_records_resources.services import (Link, LinksTemplate, Service,
                                                ServiceConfig)

#
# Data layer - data access and integrity
#
# Responsibilities:
# - data access (fetch and store)
# - data integrity

class NoResultError(Exception):
    pass

class TodoDatabase:
    db = {}

    @classmethod
    def add(cls, item):
        cls.db[item.id] = item

    @classmethod
    def get(cls, id_):
        if id_ not in cls.db:
            raise NoResultError(id_)
        return cls.db[id_]

    @classmethod
    def get_all(cls):
        return cls.db.values()


class TodoItem:
    def __init__(self, id_, title, priority, user_id):
        self.id = id_
        self.title = title
        self.priority = priority
        self.user_id = user_id


#
# Service - business logic
#
# Responsibilities:
# - High-level API and control flow
# - Authorization
# - Business-level validation
# - Uses the data access layer entities to accomplish the tasks.
# - Results are always according to a given context (e.g. a given user)

class TodoService(Service):

    def create(self, identity, data):
        # Check if the given identity is allowed to perform the given
        # operation.
        self.require_permission(identity, "create")

        # Validate data - uses a marshmallow schema to validate the incoming
        # data (e.g. the "id" and "title" keys must be present, and the default
        # value for priority is 3)
        obj = self.config.schema_cls().load(data)

        # Creates the data layer entity and commits it to the database.
        item = self.config.todo_item_cls(
            obj['id'],
            obj['title'],
            obj['priority'],
            identity.id
        )
        TodoDatabase.add(item)

        # A service NEVER returns the data layer object directly. It always
        # wraps the item in a context - we call that a service result.
        # The result is responsible for producing a result that's readable by
        # a given identity and enhance it with e.g. links.
        return self.result_item(item, identity, self.config.links_item)

    def read(self, identity, id_):
        # Retrieve the from the data layer - the data layer may throw an
        # exception which we let the view layer handle and translate into a
        # JSON response
        item = TodoDatabase.get(id_)

        # Check permission
        self.require_permission(identity, "read", item=item)

        # Return the wrapped item.
        return self.result_item(item, identity, self.config.links_item)

    def list_(self, identity):
        item_list = []
        for item in TodoDatabase.get_all():
            if self.check_permission(identity, "read", item=item):
                item_list.append(self.result_item(item, identity, self.config.links_item))

        # Return the wrapped item.
        return self.result_list(item_list)


class UnknownResultType(Exception):
    pass

class TodoItemResult:
    def __init__(self, item, identity, links_tpl):
        self._item = item
        self._identity = identity
        self._links_tpl = links_tpl

    def to_dict(self):
        return {
            "id": self._item.id,
            "title": self._item.title,
            "priority": self._item.priority,
            "is_owner": self._identity.id == self._item.user_id,
            # Links are injected with URI templates (see config below)
            "links": LinksTemplate(self._links_tpl).expand(self._item)
        }

class TodoItemResultList:
    def __init__(self, item_list=None):
        self.item_list = item_list or []
        for item in self.item_list:
            if not isinstance(item, TodoItemResult):
                    raise UnknownResultType(item)

    def to_list(self):
        item_list = []
        for item in self.item_list:
            item_list.append(item.to_dict())

        return item_list

#
# Presentation layer - RESTful resources
#
# Responsibilities
# - HTTP interface to the service - i.e. parses and translate an HTTP request
#   into a service method call.
# - Request body parsing: deserialization of the request body into the common
#   form required by the service.
# - Request (URL path, URL query string, headers) parsing.
# - Performs authentication but not authorization.


# A decorator we use to extract arguments from the request.
user_request_parser = request_parser(
    # A Marshmallow schema defines the validation rules applied.
    {'user': ma.fields.Int(missing=None)},
    # The location parameters defines from where to read the values (options
    # are args = request.args, view_args = request.view_args,
    # headers = request.headers)
    location='args',
    # Below defines what to do with unknown values (passed to marshmallow
    # schema). Either ma.EXCLUDE, ma.INCLUDE or ma.RAISE
    unknown=ma.RAISE,
)

class TodoResource(Resource):
    def __init__(self, config, service):
        super().__init__(config)
        # The service layer is injected into the resource, so that the resource
        # have a service instance to perform it's task with.
        self.service = service

    #
    # Resource API
    #
    error_handlers = {
        # Here we map data and service level exceptions into errors for the
        # user. This dictionary is passed directly to
        # Blueprint.register_error_handler().
        NoResultError: create_error_handler(
            # The HTTPJSONException is responsible for creating an HTTP
            # response with a JSON-formatted body. We do not do content
            # negotiation on errors.
            HTTPJSONException(code=404, description="Not found"),
        ),
        ma.exceptions.ValidationError: create_error_handler(
            HTTPJSONException(code=400, description="Bad request"),
        ),
        PermissionDenied: create_error_handler(
            HTTPJSONException(code=403, description="Forbidden"),
        ),
    }

    def create_url_rules(self):
        # Here we define the RESTful routes. The return value is passed
        # directly to Blueprint.add_url_rule().
        return [
            # The "route()" does a couple of things:
            # - it enforces one HTTP method = one class method (similar to
            #   flask.MethodView, however it allows many methods)
            # - it puts more emphasis on the HTTP method
            # - it wraps the resource method (e.g. self.create) in a >>resource
            #   request context<<. More on that below.
            # You are not required to use the "route()".
            route("POST", "", self.create),
            route("GET", "", self.list_),
            route("GET", "/<item_id>", self.read),
        ]

    #
    # Internals
    #
    def _make_identity(self, user_id):
        # This method is a replacement for having proper login system etc.
        if user_id is not None:
            i = Identity(user_id)
            i.provides.add(UserNeed(user_id))
            i.provides.add(RoleNeed("authenticated_user"))
            return i
        else:
            return AnonymousIdentity()

    #
    # View methods
    #
    # Most view methods looks like below:
    # - A couple of decorators to extract arguments from the HTTP request,
    # - A single call to a service method
    # - Returns a simple dict representation of their object with a HTTP status
    #   code

    # The user request parser is a decorator defined above (because we use it
    # multiple times DRY).
    @user_request_parser
    # The request body parser allows the client to send data in many different
    # data formats by sending the Content-Type header (e.g.
    # "Content-Type: application/json"). This can e.g be used for versioning
    # the REST API.
    @request_body_parser(parsers={
        "application/json": RequestBodyParser(JSONDeserializer())
    })
    # The response handler, is the decorator which allows the view to return
    # an dict object instead of a HTTP response. The response handler works in
    # conjunction with the HTTP content negotiation. That is, if a client
    # sends a "Accept: application/json" header, the response handler will
    # choose the appropriate serializer (e.g. return XML, JSON, plain text, ..)
    @response_handler()
    def create(self):
        # The view method itself, does not take any arguments. This is because
        # we ensure that all data is validated and passed through the resource
        # request context (i.e. anything that ends up in "resource_requestctx"
        # has been validated according to the rules defined).
        identity = self._make_identity(resource_requestctx.args['user'])

        item = self.service.create(
            identity,
            resource_requestctx.data,
        )
        # A view may return a dict if the @response_handler decorator was used.
        # Alternatively, the view can also simply return a normal
        # Flask.Response.
        return item.to_dict(), 201

    @user_request_parser
    @request_parser(
        # This request parser extracts the item id from the URL. The name
        # "item_id" is the one we used in create_url_rules().
        {'item_id': ma.fields.Int(required=True)},
        location='view_args',
        unknown=ma.RAISE,
    )
    @response_handler()
    def read(self):
        identity = self._make_identity(resource_requestctx.args['user'])

        item = self.service.read(
            identity,
            resource_requestctx.view_args['item_id'],
        )

        return item.to_dict(), 200

    @user_request_parser
    @response_handler()
    def list_(self):
        identity = self._make_identity(resource_requestctx.args['user'])

        item_list = self.service.list_(identity)

        return item_list.to_list(), 200


# This is the end of the three layers - presentation, service and data.
# =============================================================================

#
# Dependency injection
#
# All the classes below are objects that we inject as dependencies in either
# the service or resource.


#
# Permissions policy
#
class Owner(Generator):
    def needs(self, item=None):
        return [UserNeed(item.user_id)]


class AuthenticatedUser(Generator):
    def needs(self, item=None):
        return [RoleNeed("authenticated_user")]


class TodoPermissionPolicy(Permission):

    # Jump over the __init__ - it's a small helper method, to avoid
    # initializing the full Invenio-Access.
    def __init__(self, action, item=None):
        generators = getattr(self, f"can_{action}")
        needs = []
        for g in generators:
            for n in g.needs(item=item):
                needs.append(n)
        super().__init__(*needs)

    # A permission policy defines a declarative way of writing the permissions.
    # The "can_create" requires that a user is authenticated, thus if you don't
    # pass "?user=1" in the URL query string this permission check will fail.
    can_create = [AuthenticatedUser()]
    # The "can_read" requires that the user reading the object is the one who
    # created it.
    can_read = [Owner()]


#
# Schema
#
# The schema peforms the business level validation logic. E.g. id/title is
# required but priority is not.
class TodoSchema(ma.Schema):
    id = ma.fields.Int(required=True)
    title = ma.fields.String(required=True)
    priority = ma.fields.Int(missing=3)


#
# Configs
#
# The service and resource configs are both objects we use to inject
# dependencies in one go.
class TodoServiceConfig(ServiceConfig):
    permission_policy_cls = TodoPermissionPolicy
    result_item_cls = TodoItemResult
    result_list_cls = TodoItemResultList
    todo_item_cls = TodoItem
    schema_cls = TodoSchema

    # As an example, we define here the links injected in the HTTP JSON
    # response.
    links_item = {
        "self": Link(
            # Below is a URI template (RFC 6570) - the "+" is the syntax used
            # to not perform escaping of the variable.
            "{+api}/todos/{id}",
            vars=lambda item, vars: vars.update({"id": item.id})
        )
    }


class TodoResourceConfig(ResourceConfig):
    blueprint_name = "todo"
    url_prefix = "/todos"

    response_handlers = {
        "application/json": ResponseHandler(JSONSerializer()),
    }


#
# Application creation
#
def create_app():
    service = TodoService(TodoServiceConfig)
    resource = TodoResource(TodoResourceConfig, service)

    app = Flask('test')
    app.config.update({"SITE_API_URL": "http://127.0.0.1:5000"})
    app.register_blueprint(resource.as_blueprint())
    return app

app = create_app()
