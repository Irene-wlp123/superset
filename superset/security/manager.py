# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
# pylint: disable=too-many-lines
"""A set of constants and methods to manage permissions and security"""
import logging
import re
import time
from collections import defaultdict
from typing import (
    Any,
    Callable,
    cast,
    Dict,
    List,
    NamedTuple,
    Optional,
    Set,
    TYPE_CHECKING,
    Union,
)

from flask import current_app, Flask, g, Request
from flask_appbuilder import Model
from flask_appbuilder.models.sqla.interface import SQLAInterface
from flask_appbuilder.security.sqla.manager import SecurityManager
from flask_appbuilder.security.sqla.models import (
    assoc_permissionview_role,
    assoc_user_role,
    PermissionView,
    Role,
    User,
)
from flask_appbuilder.security.views import (
    PermissionModelView,
    PermissionViewModelView,
    RoleModelView,
    UserModelView,
    ViewMenuModelView,
)
from flask_appbuilder.widgets import ListWidget
from flask_login import AnonymousUserMixin, LoginManager
from jwt.api_jwt import _jwt_global_obj
from sqlalchemy import and_, or_
from sqlalchemy.engine.base import Connection
from sqlalchemy.orm import Session
from sqlalchemy.orm.mapper import Mapper
from sqlalchemy.orm.query import Query as SqlaQuery

from superset import sql_parse
from superset.constants import RouteMethod
from superset.errors import ErrorLevel, SupersetError, SupersetErrorType
from superset.exceptions import (
    DatasetInvalidPermissionEvaluationException,
    SupersetSecurityException,
)
from superset.security.guest_token import (
    GuestToken,
    GuestTokenResources,
    GuestTokenResourceType,
    GuestTokenRlsRule,
    GuestTokenUser,
    GuestUser,
)
from superset.utils.core import DatasourceName, get_user_id, RowLevelSecurityFilterType
from superset.utils.urls import get_url_host

if TYPE_CHECKING:
    from superset.common.query_context import QueryContext
    from superset.connectors.base.models import BaseDatasource
    from superset.models.core import Database
    from superset.models.dashboard import Dashboard
    from superset.models.sql_lab import Query
    from superset.sql_parse import Table
    from superset.viz import BaseViz

logger = logging.getLogger(__name__)


class DatabaseAndSchema(NamedTuple):
    database: str
    schema: str


class SupersetSecurityListWidget(ListWidget):  # pylint: disable=too-few-public-methods
    """
    Redeclaring to avoid circular imports
    """

    template = "superset/fab_overrides/list.html"


class SupersetRoleListWidget(ListWidget):  # pylint: disable=too-few-public-methods
    """
    Role model view from FAB already uses a custom list widget override
    So we override the override
    """

    template = "superset/fab_overrides/list_role.html"

    def __init__(self, **kwargs: Any) -> None:
        kwargs["appbuilder"] = current_app.appbuilder
        super().__init__(**kwargs)


UserModelView.list_widget = SupersetSecurityListWidget
RoleModelView.list_widget = SupersetRoleListWidget
PermissionViewModelView.list_widget = SupersetSecurityListWidget
PermissionModelView.list_widget = SupersetSecurityListWidget

# Limiting routes on FAB model views
UserModelView.include_route_methods = RouteMethod.CRUD_SET | {
    RouteMethod.ACTION,
    RouteMethod.API_READ,
    RouteMethod.ACTION_POST,
    "userinfo",
}
RoleModelView.include_route_methods = RouteMethod.CRUD_SET
PermissionViewModelView.include_route_methods = {RouteMethod.LIST}
PermissionModelView.include_route_methods = {RouteMethod.LIST}
ViewMenuModelView.include_route_methods = {RouteMethod.LIST}

RoleModelView.list_columns = ["name"]
RoleModelView.edit_columns = ["name", "permissions", "user"]
RoleModelView.related_views = []


class SupersetSecurityManager(  # pylint: disable=too-many-public-methods
    SecurityManager
):
    userstatschartview = None
    READ_ONLY_MODEL_VIEWS = {"Database", "DruidClusterModelView", "DynamicPlugin"}

    USER_MODEL_VIEWS = {
        "UserDBModelView",
        "UserLDAPModelView",
        "UserOAuthModelView",
        "UserOIDModelView",
        "UserRemoteUserModelView",
    }

    GAMMA_READ_ONLY_MODEL_VIEWS = {
        "Dataset",
        "Datasource",
    } | READ_ONLY_MODEL_VIEWS

    ADMIN_ONLY_VIEW_MENUS = {
        "AccessRequestsModelView",
        "SQL Lab",
        "Refresh Druid Metadata",
        "ResetPasswordView",
        "RoleModelView",
        "Log",
        "Security",
        "Row Level Security",
        "Row Level Security Filters",
        "RowLevelSecurityFiltersModelView",
    } | USER_MODEL_VIEWS

    ALPHA_ONLY_VIEW_MENUS = {
        "Manage",
        "CSS Templates",
        "Queries",
        "Import dashboards",
        "Upload a CSV",
    }

    ADMIN_ONLY_PERMISSIONS = {
        "can_sql_json",  # TODO: move can_sql_json to sql_lab role
        "can_override_role_permissions",
        "can_sync_druid_source",
        "can_override_role_permissions",
        "can_approve",
        "can_update_role",
        "all_query_access",
        "can_grant_guest_token",
        "can_set_embedded",
    }

    READ_ONLY_PERMISSION = {
        "can_show",
        "can_list",
        "can_get",
        "can_external_metadata",
        "can_external_metadata_by_name",
        "can_read",
    }

    ALPHA_ONLY_PERMISSIONS = {
        "muldelete",
        "all_database_access",
        "all_datasource_access",
    }

    OBJECT_SPEC_PERMISSIONS = {
        "database_access",
        "schema_access",
        "datasource_access",
    }

    ACCESSIBLE_PERMS = {"can_userinfo", "resetmypassword"}

    SQLLAB_PERMISSION_VIEWS = {
        ("can_csv", "Superset"),
        ("can_read", "SavedQuery"),
        ("can_read", "Database"),
        ("can_sql_json", "Superset"),
        ("can_sqllab_viz", "Superset"),
        ("can_sqllab_table_viz", "Superset"),
        ("can_sqllab", "Superset"),
        ("menu_access", "SQL Lab"),
        ("menu_access", "SQL Editor"),
        ("menu_access", "Saved Queries"),
        ("menu_access", "Query Search"),
    }

    data_access_permissions = (
        "database_access",
        "schema_access",
        "datasource_access",
        "all_datasource_access",
        "all_database_access",
        "all_query_access",
    )

    guest_user_cls = GuestUser
    pyjwt_for_guest_token = _jwt_global_obj

    def create_login_manager(self, app: Flask) -> LoginManager:
        lm = super().create_login_manager(app)
        lm.request_loader(self.request_loader)
        return lm

    def request_loader(self, request: Request) -> Optional[User]:
        # pylint: disable=import-outside-toplevel
        from superset.extensions import feature_flag_manager

        if feature_flag_manager.is_feature_enabled("EMBEDDED_SUPERSET"):
            return self.get_guest_user_from_request(request)
        return None

    def get_schema_perm(  # pylint: disable=no-self-use
        self, database: Union["Database", str], schema: Optional[str] = None
    ) -> Optional[str]:
        """
        Return the database specific schema permission.

        :param database: The Superset database or database name
        :param schema: The Superset schema name
        :return: The database specific schema permission
        """

        if schema:
            return f"[{database}].[{schema}]"

        return None

    def unpack_database_and_schema(  # pylint: disable=no-self-use
        self, schema_permission: str
    ) -> DatabaseAndSchema:
        # [database_name].[schema|table]

        schema_name = schema_permission.split(".")[1][1:-1]
        database_name = schema_permission.split(".")[0][1:-1]
        return DatabaseAndSchema(database_name, schema_name)

    def can_access(self, permission_name: str, view_name: str) -> bool:
        """
        Return True if the user can access the FAB permission/view, False otherwise.

        Note this method adds protection from has_access failing from missing
        permission/view entries.

        :param permission_name: The FAB permission name
        :param view_name: The FAB view-menu name
        :returns: Whether the user can access the FAB permission/view
        """

        user = g.user
        if user.is_anonymous:
            return self.is_item_public(permission_name, view_name)
        return self._has_view_access(user, permission_name, view_name)

    def can_access_all_queries(self) -> bool:
        """
        Return True if the user can access all SQL Lab queries, False otherwise.

        :returns: Whether the user can access all queries
        """

        return self.can_access("all_query_access", "all_query_access")

    def can_access_all_datasources(self) -> bool:
        """
        Return True if the user can fully access all the Superset datasources, False
        otherwise.

        :returns: Whether the user can fully access all Superset datasources
        """

        return self.can_access("all_datasource_access", "all_datasource_access")

    def can_access_all_databases(self) -> bool:
        """
        Return True if the user can fully access all the Superset databases, False
        otherwise.

        :returns: Whether the user can fully access all Superset databases
        """

        return self.can_access("all_database_access", "all_database_access")

    def can_access_database(self, database: "Database") -> bool:
        """
        Return True if the user can fully access the Superset database, False otherwise.

        Note for Druid the database is akin to the Druid cluster.

        :param database: The Superset database
        :returns: Whether the user can fully access the Superset database
        """

        return (
            self.can_access_all_datasources()
            or self.can_access_all_databases()
            or self.can_access("database_access", database.perm)  # type: ignore
        )

    def can_access_schema(self, datasource: "BaseDatasource") -> bool:
        """
        Return True if the user can fully access the schema associated with the Superset
        datasource, False otherwise.

        Note for Druid datasources the database and schema are akin to the Druid cluster
        and datasource name prefix respectively, i.e., [schema.]datasource.

        :param datasource: The Superset datasource
        :returns: Whether the user can fully access the datasource's schema
        """

        return (
            self.can_access_all_datasources()
            or self.can_access_database(datasource.database)
            or self.can_access("schema_access", datasource.schema_perm or "")
        )

    def can_access_datasource(self, datasource: "BaseDatasource") -> bool:
        """
        Return True if the user can fully access of the Superset datasource, False
        otherwise.

        :param datasource: The Superset datasource
        :returns: Whether the user can fully access the Superset datasource
        """

        try:
            self.raise_for_access(datasource=datasource)
        except SupersetSecurityException:
            return False

        return True

    @staticmethod
    def get_datasource_access_error_msg(datasource: "BaseDatasource") -> str:
        """
        Return the error message for the denied Superset datasource.

        :param datasource: The denied Superset datasource
        :returns: The error message
        """

        return f"""This endpoint requires the datasource {datasource.name}, database or
            `all_datasource_access` permission"""

    @staticmethod
    def get_datasource_access_link(  # pylint: disable=unused-argument
        datasource: "BaseDatasource",
    ) -> Optional[str]:
        """
        Return the link for the denied Superset datasource.

        :param datasource: The denied Superset datasource
        :returns: The access URL
        """

        return current_app.config.get("PERMISSION_INSTRUCTIONS_LINK")

    def get_datasource_access_error_object(  # pylint: disable=invalid-name
        self, datasource: "BaseDatasource"
    ) -> SupersetError:
        """
        Return the error object for the denied Superset datasource.

        :param datasource: The denied Superset datasource
        :returns: The error object
        """
        return SupersetError(
            error_type=SupersetErrorType.DATASOURCE_SECURITY_ACCESS_ERROR,
            message=self.get_datasource_access_error_msg(datasource),
            level=ErrorLevel.ERROR,
            extra={
                "link": self.get_datasource_access_link(datasource),
                "datasource": datasource.name,
            },
        )

    def get_table_access_error_msg(  # pylint: disable=no-self-use
        self, tables: Set["Table"]
    ) -> str:
        """
        Return the error message for the denied SQL tables.

        :param tables: The set of denied SQL tables
        :returns: The error message
        """

        quoted_tables = [f"`{table}`" for table in tables]
        return f"""You need access to the following tables: {", ".join(quoted_tables)},
            `all_database_access` or `all_datasource_access` permission"""

    def get_table_access_error_object(self, tables: Set["Table"]) -> SupersetError:
        """
        Return the error object for the denied SQL tables.

        :param tables: The set of denied SQL tables
        :returns: The error object
        """
        return SupersetError(
            error_type=SupersetErrorType.TABLE_SECURITY_ACCESS_ERROR,
            message=self.get_table_access_error_msg(tables),
            level=ErrorLevel.ERROR,
            extra={
                "link": self.get_table_access_link(tables),
                "tables": [str(table) for table in tables],
            },
        )

    def get_table_access_link(  # pylint: disable=unused-argument,no-self-use
        self, tables: Set["Table"]
    ) -> Optional[str]:
        """
        Return the access link for the denied SQL tables.

        :param tables: The set of denied SQL tables
        :returns: The access URL
        """

        return current_app.config.get("PERMISSION_INSTRUCTIONS_LINK")

    def get_user_datasources(self) -> List["BaseDatasource"]:
        """
        Collect datasources which the user has explicit permissions to.

        :returns: The list of datasources
        """

        user_perms = self.user_view_menu_names("datasource_access")
        schema_perms = self.user_view_menu_names("schema_access")
        user_datasources = set()

        # pylint: disable=import-outside-toplevel
        from superset.connectors.sqla.models import SqlaTable

        user_datasources.update(
            self.get_session.query(SqlaTable)
            .filter(
                or_(
                    SqlaTable.perm.in_(user_perms),
                    SqlaTable.schema_perm.in_(schema_perms),
                )
            )
            .all()
        )

        # group all datasources by database
        session = self.get_session
        all_datasources = SqlaTable.get_all_datasources(session)
        datasources_by_database: Dict["Database", Set["SqlaTable"]] = defaultdict(set)
        for datasource in all_datasources:
            datasources_by_database[datasource.database].add(datasource)

        # add datasources with implicit permission (eg, database access)
        for database, datasources in datasources_by_database.items():
            if self.can_access_database(database):
                user_datasources.update(datasources)

        return list(user_datasources)

    def can_access_table(self, database: "Database", table: "Table") -> bool:
        """
        Return True if the user can access the SQL table, False otherwise.

        :param database: The SQL database
        :param table: The SQL table
        :returns: Whether the user can access the SQL table
        """

        try:
            self.raise_for_access(database=database, table=table)
        except SupersetSecurityException:
            return False

        return True

    def user_view_menu_names(self, permission_name: str) -> Set[str]:
        base_query = (
            self.get_session.query(self.viewmenu_model.name)
            .join(self.permissionview_model)
            .join(self.permission_model)
            .join(assoc_permissionview_role)
            .join(self.role_model)
        )

        if not g.user.is_anonymous:
            # filter by user id
            view_menu_names = (
                base_query.join(assoc_user_role)
                .join(self.user_model)
                .filter(self.user_model.id == get_user_id())
                .filter(self.permission_model.name == permission_name)
            ).all()
            return {s.name for s in view_menu_names}

        # Properly treat anonymous user
        public_role = self.get_public_role()
        if public_role:
            # filter by public role
            view_menu_names = (
                base_query.filter(self.role_model.id == public_role.id).filter(
                    self.permission_model.name == permission_name
                )
            ).all()
            return {s.name for s in view_menu_names}
        return set()

    def get_schemas_accessible_by_user(
        self, database: "Database", schemas: List[str], hierarchical: bool = True
    ) -> List[str]:
        """
        Return the list of SQL schemas accessible by the user.

        :param database: The SQL database
        :param schemas: The list of eligible SQL schemas
        :param hierarchical: Whether to check using the hierarchical permission logic
        :returns: The list of accessible SQL schemas
        """

        # pylint: disable=import-outside-toplevel
        from superset.connectors.sqla.models import SqlaTable

        if hierarchical and self.can_access_database(database):
            return schemas

        # schema_access
        accessible_schemas = {
            self.unpack_database_and_schema(s).schema
            for s in self.user_view_menu_names("schema_access")
            if s.startswith(f"[{database}].")
        }

        # datasource_access
        perms = self.user_view_menu_names("datasource_access")
        if perms:
            tables = (
                self.get_session.query(SqlaTable.schema)
                .filter(SqlaTable.database_id == database.id)
                .filter(SqlaTable.schema.isnot(None))
                .filter(SqlaTable.schema != "")
                .filter(or_(SqlaTable.perm.in_(perms)))
                .distinct()
            )
            accessible_schemas.update([table.schema for table in tables])

        return [s for s in schemas if s in accessible_schemas]

    def get_datasources_accessible_by_user(  # pylint: disable=invalid-name
        self,
        database: "Database",
        datasource_names: List[DatasourceName],
        schema: Optional[str] = None,
    ) -> List[DatasourceName]:
        """
        Return the list of SQL tables accessible by the user.

        :param database: The SQL database
        :param datasource_names: The list of eligible SQL tables w/ schema
        :param schema: The fallback SQL schema if not present in the table name
        :returns: The list of accessible SQL tables w/ schema
        """
        # pylint: disable=import-outside-toplevel
        from superset.connectors.sqla.models import SqlaTable

        if self.can_access_database(database):
            return datasource_names

        if schema:
            schema_perm = self.get_schema_perm(database, schema)
            if schema_perm and self.can_access("schema_access", schema_perm):
                return datasource_names

        user_perms = self.user_view_menu_names("datasource_access")
        schema_perms = self.user_view_menu_names("schema_access")
        user_datasources = SqlaTable.query_datasources_by_permissions(
            self.get_session, database, user_perms, schema_perms
        )
        if schema:
            names = {d.table_name for d in user_datasources if d.schema == schema}
            return [d for d in datasource_names if d.table in names]

        full_names = {d.full_name for d in user_datasources}
        return [d for d in datasource_names if f"[{database}].[{d}]" in full_names]

    def merge_perm(self, permission_name: str, view_menu_name: str) -> None:
        """
        Add the FAB permission/view-menu.

        :param permission_name: The FAB permission name
        :param view_menu_names: The FAB view-menu name
        :see: SecurityManager.add_permission_view_menu
        """

        logger.warning(
            "This method 'merge_perm' is deprecated use add_permission_view_menu"
        )
        self.add_permission_view_menu(permission_name, view_menu_name)

    def _is_user_defined_permission(self, perm: Model) -> bool:
        """
        Return True if the FAB permission is user defined, False otherwise.

        :param perm: The FAB permission
        :returns: Whether the FAB permission is user defined
        """

        return perm.permission.name in self.OBJECT_SPEC_PERMISSIONS

    def create_custom_permissions(self) -> None:
        """
        Create custom FAB permissions.
        """
        self.add_permission_view_menu("all_datasource_access", "all_datasource_access")
        self.add_permission_view_menu("all_database_access", "all_database_access")
        self.add_permission_view_menu("all_query_access", "all_query_access")
        self.add_permission_view_menu("can_share_dashboard", "Superset")
        self.add_permission_view_menu("can_share_chart", "Superset")

    def create_missing_perms(self) -> None:
        """
        Creates missing FAB permissions for datasources, schemas and metrics.
        """

        # pylint: disable=import-outside-toplevel
        from superset.connectors.sqla.models import SqlaTable
        from superset.models import core as models

        logger.info("Fetching a set of all perms to lookup which ones are missing")
        all_pvs = set()
        for pv in self.get_session.query(self.permissionview_model).all():
            if pv.permission and pv.view_menu:
                all_pvs.add((pv.permission.name, pv.view_menu.name))

        def merge_pv(view_menu: str, perm: Optional[str]) -> None:
            """Create permission view menu only if it doesn't exist"""
            if view_menu and perm and (view_menu, perm) not in all_pvs:
                self.add_permission_view_menu(view_menu, perm)

        logger.info("Creating missing datasource permissions.")
        datasources = SqlaTable.get_all_datasources(self.get_session)
        for datasource in datasources:
            merge_pv("datasource_access", datasource.get_perm())
            merge_pv("schema_access", datasource.get_schema_perm())

        logger.info("Creating missing database permissions.")
        databases = self.get_session.query(models.Database).all()
        for database in databases:
            merge_pv("database_access", database.perm)

    def clean_perms(self) -> None:
        """
        Clean up the FAB faulty permissions.
        """

        logger.info("Cleaning faulty perms")
        sesh = self.get_session
        pvms = sesh.query(PermissionView).filter(
            or_(
                PermissionView.permission  # pylint: disable=singleton-comparison
                == None,
                PermissionView.view_menu  # pylint: disable=singleton-comparison
                == None,
            )
        )
        deleted_count = pvms.delete()
        sesh.commit()
        if deleted_count:
            logger.info("Deleted %i faulty permissions", deleted_count)

    def sync_role_definitions(self) -> None:
        """
        Initialize the Superset application with security roles and such.
        """

        logger.info("Syncing role definition")

        self.create_custom_permissions()

        # Creating default roles
        self.set_role("Admin", self._is_admin_pvm)
        self.set_role("Alpha", self._is_alpha_pvm)
        self.set_role("Gamma", self._is_gamma_pvm)
        self.set_role("granter", self._is_granter_pvm)
        self.set_role("sql_lab", self._is_sql_lab_pvm)

        # Configure public role
        if current_app.config["PUBLIC_ROLE_LIKE"]:
            self.copy_role(
                current_app.config["PUBLIC_ROLE_LIKE"],
                self.auth_role_public,
                merge=True,
            )

        self.create_missing_perms()

        # commit role and view menu updates
        self.get_session.commit()
        self.clean_perms()

    def _get_pvms_from_builtin_role(self, role_name: str) -> List[PermissionView]:
        """
        Gets a list of model PermissionView permissions infered from a builtin role
        definition
        """
        role_from_permissions_names = self.builtin_roles.get(role_name, [])
        all_pvms = self.get_session.query(PermissionView).all()
        role_from_permissions = []
        for pvm_regex in role_from_permissions_names:
            view_name_regex = pvm_regex[0]
            permission_name_regex = pvm_regex[1]
            for pvm in all_pvms:
                if re.match(view_name_regex, pvm.view_menu.name) and re.match(
                    permission_name_regex, pvm.permission.name
                ):
                    if pvm not in role_from_permissions:
                        role_from_permissions.append(pvm)
        return role_from_permissions

    def find_roles_by_id(self, role_ids: List[int]) -> List[Role]:
        """
        Find a List of models by a list of ids, if defined applies `base_filter`
        """
        query = self.get_session.query(Role).filter(Role.id.in_(role_ids))
        return query.all()

    def copy_role(
        self, role_from_name: str, role_to_name: str, merge: bool = True
    ) -> None:
        """
        Copies permissions from a role to another.

        Note: Supports regex defined builtin roles

        :param role_from_name: The FAB role name from where the permissions are taken
        :param role_to_name: The FAB role name from where the permissions are copied to
        :param merge: If merge is true, keep data access permissions
            if they already exist on the target role
        """

        logger.info("Copy/Merge %s to %s", role_from_name, role_to_name)
        # If it's a builtin role extract permissions from it
        if role_from_name in self.builtin_roles:
            role_from_permissions = self._get_pvms_from_builtin_role(role_from_name)
        else:
            role_from_permissions = list(self.find_role(role_from_name).permissions)
        role_to = self.add_role(role_to_name)
        # If merge, recover existing data access permissions
        if merge:
            for permission_view in role_to.permissions:
                if (
                    permission_view not in role_from_permissions
                    and permission_view.permission.name in self.data_access_permissions
                ):
                    role_from_permissions.append(permission_view)
        role_to.permissions = role_from_permissions
        self.get_session.merge(role_to)
        self.get_session.commit()

    def set_role(
        self, role_name: str, pvm_check: Callable[[PermissionView], bool]
    ) -> None:
        """
        Set the FAB permission/views for the role.

        :param role_name: The FAB role name
        :param pvm_check: The FAB permission/view check
        """

        logger.info("Syncing %s perms", role_name)
        pvms = self.get_session.query(PermissionView).all()
        pvms = [p for p in pvms if p.permission and p.view_menu]
        role = self.add_role(role_name)
        role_pvms = [
            permission_view for permission_view in pvms if pvm_check(permission_view)
        ]
        role.permissions = role_pvms
        self.get_session.merge(role)
        self.get_session.commit()

    def _is_admin_only(self, pvm: PermissionView) -> bool:
        """
        Return True if the FAB permission/view is accessible to only Admin users,
        False otherwise.

        Note readonly operations on read only model views are allowed only for admins.

        :param pvm: The FAB permission/view
        :returns: Whether the FAB object is accessible to only Admin users
        """

        if (
            pvm.view_menu.name in self.READ_ONLY_MODEL_VIEWS
            and pvm.permission.name not in self.READ_ONLY_PERMISSION
        ):
            return True
        return (
            pvm.view_menu.name in self.ADMIN_ONLY_VIEW_MENUS
            or pvm.permission.name in self.ADMIN_ONLY_PERMISSIONS
        )

    def _is_alpha_only(self, pvm: PermissionView) -> bool:
        """
        Return True if the FAB permission/view is accessible to only Alpha users,
        False otherwise.

        :param pvm: The FAB permission/view
        :returns: Whether the FAB object is accessible to only Alpha users
        """

        if (
            pvm.view_menu.name in self.GAMMA_READ_ONLY_MODEL_VIEWS
            and pvm.permission.name not in self.READ_ONLY_PERMISSION
        ):
            return True
        return (
            pvm.view_menu.name in self.ALPHA_ONLY_VIEW_MENUS
            or pvm.permission.name in self.ALPHA_ONLY_PERMISSIONS
        )

    def _is_accessible_to_all(self, pvm: PermissionView) -> bool:
        """
        Return True if the FAB permission/view is accessible to all, False
        otherwise.

        :param pvm: The FAB permission/view
        :returns: Whether the FAB object is accessible to all users
        """

        return pvm.permission.name in self.ACCESSIBLE_PERMS

    def _is_admin_pvm(self, pvm: PermissionView) -> bool:
        """
        Return True if the FAB permission/view is Admin user related, False
        otherwise.

        :param pvm: The FAB permission/view
        :returns: Whether the FAB object is Admin related
        """

        return not self._is_user_defined_permission(pvm)

    def _is_alpha_pvm(self, pvm: PermissionView) -> bool:
        """
        Return True if the FAB permission/view is Alpha user related, False
        otherwise.

        :param pvm: The FAB permission/view
        :returns: Whether the FAB object is Alpha related
        """

        return not (
            self._is_user_defined_permission(pvm) or self._is_admin_only(pvm)
        ) or self._is_accessible_to_all(pvm)

    def _is_gamma_pvm(self, pvm: PermissionView) -> bool:
        """
        Return True if the FAB permission/view is Gamma user related, False
        otherwise.

        :param pvm: The FAB permission/view
        :returns: Whether the FAB object is Gamma related
        """

        return not (
            self._is_user_defined_permission(pvm)
            or self._is_admin_only(pvm)
            or self._is_alpha_only(pvm)
        ) or self._is_accessible_to_all(pvm)

    def _is_sql_lab_pvm(self, pvm: PermissionView) -> bool:
        """
        Return True if the FAB permission/view is SQL Lab related, False
        otherwise.

        :param pvm: The FAB permission/view
        :returns: Whether the FAB object is SQL Lab related
        """
        return (pvm.permission.name, pvm.view_menu.name) in self.SQLLAB_PERMISSION_VIEWS

    def _is_granter_pvm(  # pylint: disable=no-self-use
        self, pvm: PermissionView
    ) -> bool:
        """
        Return True if the user can grant the FAB permission/view, False
        otherwise.

        :param pvm: The FAB permission/view
        :returns: Whether the user can grant the FAB permission/view
        """

        return pvm.permission.name in {"can_override_role_permissions", "can_approve"}

    def set_perm(  # pylint: disable=unused-argument
        self, mapper: Mapper, connection: Connection, target: "BaseDatasource"
    ) -> None:
        """
        Set the datasource permissions.

        :param mapper: The table mapper
        :param connection: The DB-API connection
        :param target: The mapped instance being persisted
        """
        try:
            target_get_perm = target.get_perm()
        except DatasetInvalidPermissionEvaluationException:
            logger.warning("Dataset has no database refusing to set permission")
            return
        link_table = target.__table__
        if target.perm != target_get_perm:
            connection.execute(
                link_table.update()
                .where(link_table.c.id == target.id)
                .values(perm=target_get_perm)
            )
            target.perm = target_get_perm

        if (
            hasattr(target, "schema_perm")
            and target.schema_perm != target.get_schema_perm()
        ):
            connection.execute(
                link_table.update()
                .where(link_table.c.id == target.id)
                .values(schema_perm=target.get_schema_perm())
            )
            target.schema_perm = target.get_schema_perm()

        pvm_names = []
        if target.__tablename__ in {"dbs", "clusters"}:
            pvm_names.append(("database_access", target_get_perm))
        else:
            pvm_names.append(("datasource_access", target_get_perm))
            if target.schema:
                pvm_names.append(("schema_access", target.get_schema_perm()))

        # TODO(bogdan): modify slice permissions as well.
        for permission_name, view_menu_name in pvm_names:
            permission = self.find_permission(permission_name)
            view_menu = self.find_view_menu(view_menu_name)
            pv = None

            if not permission:
                permission_table = (
                    self.permission_model.__table__  # pylint: disable=no-member
                )
                connection.execute(
                    permission_table.insert().values(name=permission_name)
                )
                permission = self.find_permission(permission_name)
            if not view_menu:
                view_menu_table = (
                    self.viewmenu_model.__table__  # pylint: disable=no-member
                )
                connection.execute(view_menu_table.insert().values(name=view_menu_name))
                view_menu = self.find_view_menu(view_menu_name)

            if permission and view_menu:
                pv = (
                    self.get_session.query(self.permissionview_model)
                    .filter_by(permission=permission, view_menu=view_menu)
                    .first()
                )
            if not pv and permission and view_menu:
                permission_view_table = (
                    self.permissionview_model.__table__  # pylint: disable=no-member
                )
                connection.execute(
                    permission_view_table.insert().values(
                        permission_id=permission.id, view_menu_id=view_menu.id
                    )
                )

    def raise_for_access(
        # pylint: disable=too-many-arguments,too-many-locals
        self,
        database: Optional["Database"] = None,
        datasource: Optional["BaseDatasource"] = None,
        query: Optional["Query"] = None,
        query_context: Optional["QueryContext"] = None,
        table: Optional["Table"] = None,
        viz: Optional["BaseViz"] = None,
    ) -> None:
        """
        Raise an exception if the user cannot access the resource.

        :param database: The Superset database
        :param datasource: The Superset datasource
        :param query: The SQL Lab query
        :param query_context: The query context
        :param table: The Superset table (requires database)
        :param viz: The visualization
        :raises SupersetSecurityException: If the user cannot access the resource
        """

        # pylint: disable=import-outside-toplevel
        from superset.connectors.sqla.models import SqlaTable
        from superset.extensions import feature_flag_manager
        from superset.sql_parse import Table

        if database and table or query:
            if query:
                database = query.database

            database = cast("Database", database)

            if self.can_access_database(database):
                return

            if query:
                tables = {
                    Table(table_.table, table_.schema or query.schema)
                    for table_ in sql_parse.ParsedQuery(query.sql).tables
                }
            elif table:
                tables = {table}

            denied = set()

            for table_ in tables:
                schema_perm = self.get_schema_perm(database, schema=table_.schema)

                if not (schema_perm and self.can_access("schema_access", schema_perm)):
                    datasources = SqlaTable.query_datasources_by_name(
                        self.get_session, database, table_.table, schema=table_.schema
                    )

                    # Access to any datasource is suffice.
                    for datasource_ in datasources:
                        if self.can_access("datasource_access", datasource_.perm):
                            break
                    else:
                        denied.add(table_)

            if denied:
                raise SupersetSecurityException(
                    self.get_table_access_error_object(denied)
                )

        if datasource or query_context or viz:
            if query_context:
                datasource = query_context.datasource
            elif viz:
                datasource = viz.datasource

            assert datasource

            should_check_dashboard_access = (
                feature_flag_manager.is_feature_enabled("DASHBOARD_RBAC")
                or self.is_guest_user()
            )

            if not (
                self.can_access_schema(datasource)
                or self.can_access("datasource_access", datasource.perm or "")
                or (
                    should_check_dashboard_access
                    and self.can_access_based_on_dashboard(datasource)
                )
            ):
                raise SupersetSecurityException(
                    self.get_datasource_access_error_object(datasource)
                )

    def get_user_by_username(
        self, username: str, session: Session = None
    ) -> Optional[User]:
        """
        Retrieves a user by it's username case sensitive. Optional session parameter
        utility method normally useful for celery tasks where the session
        need to be scoped
        """
        session = session or self.get_session
        return (
            session.query(self.user_model)
            .filter(self.user_model.username == username)
            .one_or_none()
        )

    def get_anonymous_user(self) -> User:  # pylint: disable=no-self-use
        return AnonymousUserMixin()

    def get_user_roles(self, user: Optional[User] = None) -> List[Role]:
        if not user:
            user = g.user
        if user.is_anonymous:
            public_role = current_app.config.get("AUTH_ROLE_PUBLIC")
            return [self.get_public_role()] if public_role else []
        return user.roles

    def get_guest_rls_filters(
        self, dataset: "BaseDatasource"
    ) -> List[GuestTokenRlsRule]:
        """
        Retrieves the row level security filters for the current user and the dataset,
        if the user is authenticated with a guest token.
        :param dataset: The dataset to check against
        :return: A list of filters
        """
        guest_user = self.get_current_guest_user_if_guest()
        if guest_user:
            return [
                rule
                for rule in guest_user.rls
                if not rule.get("dataset")
                or str(rule.get("dataset")) == str(dataset.id)
            ]
        return []

    def get_rls_filters(self, table: "BaseDatasource") -> List[SqlaQuery]:
        """
        Retrieves the appropriate row level security filters for the current user and
        the passed table.

        :param table: The table to check against
        :returns: A list of filters
        """

        if not (hasattr(g, "user") and g.user is not None):
            return []

        # pylint: disable=import-outside-toplevel
        from superset.connectors.sqla.models import (
            RLSFilterRoles,
            RLSFilterTables,
            RowLevelSecurityFilter,
        )

        user_roles = [role.id for role in self.get_user_roles(g.user)]
        regular_filter_roles = (
            self.get_session()
            .query(RLSFilterRoles.c.rls_filter_id)
            .join(RowLevelSecurityFilter)
            .filter(
                RowLevelSecurityFilter.filter_type == RowLevelSecurityFilterType.REGULAR
            )
            .filter(RLSFilterRoles.c.role_id.in_(user_roles))
            .subquery()
        )
        base_filter_roles = (
            self.get_session()
            .query(RLSFilterRoles.c.rls_filter_id)
            .join(RowLevelSecurityFilter)
            .filter(
                RowLevelSecurityFilter.filter_type == RowLevelSecurityFilterType.BASE
            )
            .filter(RLSFilterRoles.c.role_id.in_(user_roles))
            .subquery()
        )
        filter_tables = (
            self.get_session()
            .query(RLSFilterTables.c.rls_filter_id)
            .filter(RLSFilterTables.c.table_id == table.id)
            .subquery()
        )
        query = (
            self.get_session()
            .query(
                RowLevelSecurityFilter.id,
                RowLevelSecurityFilter.group_key,
                RowLevelSecurityFilter.clause,
            )
            .filter(RowLevelSecurityFilter.id.in_(filter_tables))
            .filter(
                or_(
                    and_(
                        RowLevelSecurityFilter.filter_type
                        == RowLevelSecurityFilterType.REGULAR,
                        RowLevelSecurityFilter.id.in_(regular_filter_roles),
                    ),
                    and_(
                        RowLevelSecurityFilter.filter_type
                        == RowLevelSecurityFilterType.BASE,
                        RowLevelSecurityFilter.id.notin_(base_filter_roles),
                    ),
                )
            )
        )
        return query.all()

    def get_rls_ids(self, table: "BaseDatasource") -> List[int]:
        """
        Retrieves the appropriate row level security filters IDs for the current user
        and the passed table.

        :param table: The table to check against
        :returns: A list of IDs
        """
        ids = [f.id for f in self.get_rls_filters(table)]
        ids.sort()  # Combinations rather than permutations
        return ids

    def get_guest_rls_filters_str(self, table: "BaseDatasource") -> List[str]:
        return [f.get("clause", "") for f in self.get_guest_rls_filters(table)]

    def get_rls_cache_key(self, datasource: "BaseDatasource") -> List[str]:
        rls_ids = []
        if datasource.is_rls_supported:
            rls_ids = self.get_rls_ids(datasource)
        rls_str = [str(rls_id) for rls_id in rls_ids]
        guest_rls = self.get_guest_rls_filters_str(datasource)
        return guest_rls + rls_str

    @staticmethod
    def raise_for_user_activity_access(user_id: int) -> None:
        if not get_user_id() or (
            not current_app.config["ENABLE_BROAD_ACTIVITY_ACCESS"]
            and user_id != get_user_id()
        ):
            raise SupersetSecurityException(
                SupersetError(
                    error_type=SupersetErrorType.USER_ACTIVITY_SECURITY_ACCESS_ERROR,
                    message="Access to user's activity data is restricted",
                    level=ErrorLevel.ERROR,
                )
            )

    def raise_for_dashboard_access(self, dashboard: "Dashboard") -> None:
        """
        Raise an exception if the user cannot access the dashboard.
        This does not check for the required role/permission pairs,
        it only concerns itself with entity relationships.

        :param dashboard: Dashboard the user wants access to
        :raises DashboardAccessDeniedError: If the user cannot access the resource
        """
        # pylint: disable=import-outside-toplevel
        from superset import is_feature_enabled
        from superset.dashboards.commands.exceptions import DashboardAccessDeniedError
        from superset.views.base import is_user_admin
        from superset.views.utils import is_owner

        def has_rbac_access() -> bool:
            return (not is_feature_enabled("DASHBOARD_RBAC")) or any(
                dashboard_role.id
                in [user_role.id for user_role in self.get_user_roles()]
                for dashboard_role in dashboard.roles
            )

        if self.is_guest_user() and dashboard.embedded:
            can_access = self.has_guest_access(dashboard)
        else:
            can_access = (
                is_user_admin()
                or is_owner(dashboard, g.user)
                or (dashboard.published and has_rbac_access())
                or (not dashboard.published and not dashboard.roles)
            )

        if not can_access:
            raise DashboardAccessDeniedError()

    @staticmethod
    def can_access_based_on_dashboard(datasource: "BaseDatasource") -> bool:
        # pylint: disable=import-outside-toplevel
        from superset import db
        from superset.dashboards.filters import DashboardAccessFilter
        from superset.models.dashboard import Dashboard
        from superset.models.slice import Slice

        datasource_class = type(datasource)
        query = (
            db.session.query(datasource_class)
            .join(Slice.table)
            .filter(datasource_class.id == datasource.id)
        )

        query = DashboardAccessFilter("id", SQLAInterface(Dashboard, db.session)).apply(
            query, None
        )

        exists = db.session.query(query.exists()).scalar()
        return exists

    @staticmethod
    def _get_current_epoch_time() -> float:
        """This is used so the tests can mock time"""
        return time.time()

    @staticmethod
    def _get_guest_token_jwt_audience() -> str:
        audience = current_app.config["GUEST_TOKEN_JWT_AUDIENCE"] or get_url_host()
        if callable(audience):
            audience = audience()
        return audience

    @staticmethod
    def validate_guest_token_resources(resources: GuestTokenResources) -> None:
        # pylint: disable=import-outside-toplevel
        from superset.embedded.dao import EmbeddedDAO
        from superset.embedded_dashboard.commands.exceptions import (
            EmbeddedDashboardNotFoundError,
        )
        from superset.models.dashboard import Dashboard

        for resource in resources:
            if resource["type"] == GuestTokenResourceType.DASHBOARD.value:
                # TODO (embedded): remove this check once uuids are rolled out
                dashboard = Dashboard.get(str(resource["id"]))
                if not dashboard:
                    embedded = EmbeddedDAO.find_by_id(str(resource["id"]))
                    if not embedded:
                        raise EmbeddedDashboardNotFoundError()

    def create_guest_access_token(
        self,
        user: GuestTokenUser,
        resources: GuestTokenResources,
        rls: List[GuestTokenRlsRule],
    ) -> bytes:
        secret = current_app.config["GUEST_TOKEN_JWT_SECRET"]
        algo = current_app.config["GUEST_TOKEN_JWT_ALGO"]
        exp_seconds = current_app.config["GUEST_TOKEN_JWT_EXP_SECONDS"]
        audience = self._get_guest_token_jwt_audience()
        # calculate expiration time
        now = self._get_current_epoch_time()
        exp = now + exp_seconds
        claims = {
            "user": user,
            "resources": resources,
            "rls_rules": rls,
            # standard jwt claims:
            "iat": now,  # issued at
            "exp": exp,  # expiration time
            "aud": audience,
            "type": "guest",
        }
        token = self.pyjwt_for_guest_token.encode(claims, secret, algorithm=algo)
        return token

    def get_guest_user_from_request(self, req: Request) -> Optional[GuestUser]:
        """
        If there is a guest token in the request (used for embedded),
        parses the token and returns the guest user.
        This is meant to be used as a request loader for the LoginManager.
        The LoginManager will only call this if an active session cannot be found.

        :return: A guest user object
        """
        raw_token = req.headers.get(
            current_app.config["GUEST_TOKEN_HEADER_NAME"]
        ) or req.form.get("guest_token")
        if raw_token is None:
            return None

        try:
            token = self.parse_jwt_guest_token(raw_token)
            if token.get("user") is None:
                raise ValueError("Guest token does not contain a user claim")
            if token.get("resources") is None:
                raise ValueError("Guest token does not contain a resources claim")
            if token.get("rls_rules") is None:
                raise ValueError("Guest token does not contain an rls_rules claim")
            if token.get("type") != "guest":
                raise ValueError("This is not a guest token.")
        except Exception:  # pylint: disable=broad-except
            # The login manager will handle sending 401s.
            # We don't need to send a special error message.
            logger.warning("Invalid guest token", exc_info=True)
            return None
        else:
            return self.get_guest_user_from_token(cast(GuestToken, token))

    def get_guest_user_from_token(self, token: GuestToken) -> GuestUser:
        return self.guest_user_cls(
            token=token,
            roles=[self.find_role(current_app.config["GUEST_ROLE_NAME"])],
        )

    def parse_jwt_guest_token(self, raw_token: str) -> Dict[str, Any]:
        """
        Parses a guest token. Raises an error if the jwt fails standard claims checks.
        :param raw_token: the token gotten from the request
        :return: the same token that was passed in, tested but unchanged
        """
        secret = current_app.config["GUEST_TOKEN_JWT_SECRET"]
        algo = current_app.config["GUEST_TOKEN_JWT_ALGO"]
        audience = self._get_guest_token_jwt_audience()
        return self.pyjwt_for_guest_token.decode(
            raw_token, secret, algorithms=[algo], audience=audience
        )

    @staticmethod
    def is_guest_user(user: Optional[Any] = None) -> bool:
        # pylint: disable=import-outside-toplevel
        from superset import is_feature_enabled

        if not is_feature_enabled("EMBEDDED_SUPERSET"):
            return False
        if not user:
            user = g.user
        return hasattr(user, "is_guest_user") and user.is_guest_user

    def get_current_guest_user_if_guest(self) -> Optional[GuestUser]:

        if self.is_guest_user():
            return g.user
        return None

    def has_guest_access(self, dashboard: "Dashboard") -> bool:
        user = self.get_current_guest_user_if_guest()
        if not user:
            return False

        dashboards = [
            r
            for r in user.resources
            if r["type"] == GuestTokenResourceType.DASHBOARD.value
        ]

        # TODO (embedded): remove this check once uuids are rolled out
        for resource in dashboards:
            if str(resource["id"]) == str(dashboard.id):
                return True

        if not dashboard.embedded:
            return False

        for resource in dashboards:
            if str(resource["id"]) == str(dashboard.embedded[0].uuid):
                return True
        return False
