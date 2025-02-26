# Copyright 2019 The Feast Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from google.protobuf.internal.containers import RepeatedCompositeFieldContainer
from google.protobuf.json_format import MessageToDict
from proto import Message

from feast import importer
from feast.base_feature_view import BaseFeatureView
from feast.diff.FcoDiff import (
    FcoDiff,
    RegistryDiff,
    TransitionType,
    diff_between,
    tag_proto_objects_for_keep_delete_add,
)
from feast.entity import Entity
from feast.errors import (
    ConflictingFeatureViewNames,
    EntityNotFoundException,
    FeatureServiceNotFoundException,
    FeatureViewNotFoundException,
    OnDemandFeatureViewNotFoundException,
)
from feast.feature_service import FeatureService
from feast.feature_view import FeatureView
from feast.infra.infra_object import Infra
from feast.on_demand_feature_view import OnDemandFeatureView
from feast.protos.feast.core.Registry_pb2 import Registry as RegistryProto
from feast.registry_store import NoopRegistryStore
from feast.repo_config import RegistryConfig
from feast.request_feature_view import RequestFeatureView

REGISTRY_SCHEMA_VERSION = "1"


REGISTRY_STORE_CLASS_FOR_TYPE = {
    "GCSRegistryStore": "feast.infra.gcp.GCSRegistryStore",
    "S3RegistryStore": "feast.infra.aws.S3RegistryStore",
    "LocalRegistryStore": "feast.infra.local.LocalRegistryStore",
}

REGISTRY_STORE_CLASS_FOR_SCHEME = {
    "gs": "GCSRegistryStore",
    "s3": "S3RegistryStore",
    "file": "LocalRegistryStore",
    "": "LocalRegistryStore",
}

logger = logging.getLogger(__name__)


def get_registry_store_class_from_type(registry_store_type: str):
    if not registry_store_type.endswith("RegistryStore"):
        raise Exception('Registry store class name should end with "RegistryStore"')
    if registry_store_type in REGISTRY_STORE_CLASS_FOR_TYPE:
        registry_store_type = REGISTRY_STORE_CLASS_FOR_TYPE[registry_store_type]
    module_name, registry_store_class_name = registry_store_type.rsplit(".", 1)

    return importer.get_class_from_type(
        module_name, registry_store_class_name, "RegistryStore"
    )


def get_registry_store_class_from_scheme(registry_path: str):
    uri = urlparse(registry_path)
    if uri.scheme not in REGISTRY_STORE_CLASS_FOR_SCHEME:
        raise Exception(
            f"Registry path {registry_path} has unsupported scheme {uri.scheme}. "
            f"Supported schemes are file, s3 and gs."
        )
    else:
        registry_store_type = REGISTRY_STORE_CLASS_FOR_SCHEME[uri.scheme]
        return get_registry_store_class_from_type(registry_store_type)


class Registry:
    """
    Registry: A registry allows for the management and persistence of feature definitions and related metadata.
    """

    # The cached_registry_proto object is used for both reads and writes. In particular,
    # all write operations refresh the cache and modify it in memory; the write must
    # then be persisted to the underlying RegistryStore with a call to commit().
    cached_registry_proto: Optional[RegistryProto] = None
    cached_registry_proto_created: Optional[datetime] = None
    cached_registry_proto_ttl: timedelta

    def __init__(
        self, registry_config: Optional[RegistryConfig], repo_path: Optional[Path]
    ):
        """
        Create the Registry object.

        Args:
            registry_config: RegistryConfig object containing the destination path and cache ttl,
            repo_path: Path to the base of the Feast repository
            or where it will be created if it does not exist yet.
        """

        self._refresh_lock = Lock()

        if registry_config:
            registry_store_type = registry_config.registry_store_type
            registry_path = registry_config.path
            if registry_store_type is None:
                cls = get_registry_store_class_from_scheme(registry_path)
            else:
                cls = get_registry_store_class_from_type(str(registry_store_type))

            self._registry_store = cls(registry_config, repo_path)
            self.cached_registry_proto_ttl = timedelta(
                seconds=registry_config.cache_ttl_seconds
                if registry_config.cache_ttl_seconds is not None
                else 0
            )

    def clone(self) -> "Registry":
        new_registry = Registry(None, None)
        new_registry.cached_registry_proto_ttl = timedelta(seconds=0)
        new_registry.cached_registry_proto = (
            self.cached_registry_proto.__deepcopy__()
            if self.cached_registry_proto
            else RegistryProto()
        )
        new_registry.cached_registry_proto_created = datetime.utcnow()
        new_registry._registry_store = NoopRegistryStore()
        return new_registry

    # TODO(achals): This method needs to be filled out and used in the feast plan/apply methods.
    @staticmethod
    def diff_between(
        current_registry: RegistryProto, new_registry: RegistryProto
    ) -> RegistryDiff:
        diff = RegistryDiff()

        attribute_to_object_type_str = {
            "entities": "entity",
            "feature_views": "feature view",
            "feature_tables": "feature table",
            "on_demand_feature_views": "on demand feature view",
            "request_feature_views": "request feature view",
            "feature_services": "feature service",
        }

        for object_type in [
            "entities",
            "feature_views",
            "feature_tables",
            "on_demand_feature_views",
            "request_feature_views",
            "feature_services",
        ]:
            (
                objects_to_keep,
                objects_to_delete,
                objects_to_add,
            ) = tag_proto_objects_for_keep_delete_add(
                getattr(current_registry, object_type),
                getattr(new_registry, object_type),
            )

            for e in objects_to_add:
                diff.add_fco_diff(
                    FcoDiff(
                        e.spec.name,
                        attribute_to_object_type_str[object_type],
                        None,
                        e,
                        [],
                        TransitionType.CREATE,
                    )
                )
            for e in objects_to_delete:
                diff.add_fco_diff(
                    FcoDiff(
                        e.spec.name,
                        attribute_to_object_type_str[object_type],
                        e,
                        None,
                        [],
                        TransitionType.DELETE,
                    )
                )
            for e in objects_to_keep:
                current_obj_proto = [
                    _e
                    for _e in getattr(current_registry, object_type)
                    if _e.spec.name == e.spec.name
                ][0]
                diff.add_fco_diff(
                    diff_between(
                        current_obj_proto, e, attribute_to_object_type_str[object_type]
                    )
                )

        return diff

    def _initialize_registry(self):
        """Explicitly initializes the registry with an empty proto if it doesn't exist."""
        try:
            self._get_registry_proto()
        except FileNotFoundError:
            registry_proto = RegistryProto()
            registry_proto.registry_schema_version = REGISTRY_SCHEMA_VERSION
            self._registry_store.update_registry_proto(registry_proto)

    def update_infra(self, infra: Infra, project: str, commit: bool = True):
        """
        Updates the stored Infra object.

        Args:
            infra: The new Infra object to be stored.
            project: Feast project that the Infra object refers to
            commit: Whether the change should be persisted immediately
        """
        self._prepare_registry_for_changes()
        assert self.cached_registry_proto

        self.cached_registry_proto.infra.CopyFrom(infra.to_proto())
        if commit:
            self.commit()

    def get_infra(self, project: str, allow_cache: bool = False) -> Infra:
        """
        Retrieves the stored Infra object.

        Args:
            project: Feast project that the Infra object refers to
            allow_cache: Whether to allow returning this entity from a cached registry

        Returns:
            The stored Infra object.
        """
        registry_proto = self._get_registry_proto(allow_cache=allow_cache)
        return Infra.from_proto(registry_proto.infra)

    def apply_entity(self, entity: Entity, project: str, commit: bool = True):
        """
        Registers a single entity with Feast

        Args:
            entity: Entity that will be registered
            project: Feast project that this entity belongs to
            commit: Whether the change should be persisted immediately
        """
        entity.is_valid()
        entity_proto = entity.to_proto()
        entity_proto.spec.project = project
        self._prepare_registry_for_changes()
        assert self.cached_registry_proto

        for idx, existing_entity_proto in enumerate(
            self.cached_registry_proto.entities
        ):
            if (
                existing_entity_proto.spec.name == entity_proto.spec.name
                and existing_entity_proto.spec.project == project
            ):
                del self.cached_registry_proto.entities[idx]
                break

        self.cached_registry_proto.entities.append(entity_proto)
        if commit:
            self.commit()

    def list_entities(self, project: str, allow_cache: bool = False) -> List[Entity]:
        """
        Retrieve a list of entities from the registry

        Args:
            allow_cache: Whether to allow returning entities from a cached registry
            project: Filter entities based on project name

        Returns:
            List of entities
        """
        registry_proto = self._get_registry_proto(allow_cache=allow_cache)
        entities = []
        for entity_proto in registry_proto.entities:
            if entity_proto.spec.project == project:
                entities.append(Entity.from_proto(entity_proto))
        return entities

    def apply_feature_service(
        self, feature_service: FeatureService, project: str, commit: bool = True
    ):
        """
        Registers a single feature service with Feast

        Args:
            feature_service: A feature service that will be registered
            project: Feast project that this entity belongs to
        """
        feature_service_proto = feature_service.to_proto()
        feature_service_proto.spec.project = project

        registry = self._prepare_registry_for_changes()

        for idx, existing_feature_service_proto in enumerate(registry.feature_services):
            if (
                existing_feature_service_proto.spec.name
                == feature_service_proto.spec.name
                and existing_feature_service_proto.spec.project == project
            ):
                del registry.feature_services[idx]
        registry.feature_services.append(feature_service_proto)
        if commit:
            self.commit()

    def list_feature_services(
        self, project: str, allow_cache: bool = False
    ) -> List[FeatureService]:
        """
        Retrieve a list of feature services from the registry

        Args:
            allow_cache: Whether to allow returning entities from a cached registry
            project: Filter entities based on project name

        Returns:
            List of feature services
        """

        registry = self._get_registry_proto(allow_cache=allow_cache)
        feature_services = []
        for feature_service_proto in registry.feature_services:
            if feature_service_proto.spec.project == project:
                feature_services.append(
                    FeatureService.from_proto(feature_service_proto)
                )
        return feature_services

    def get_feature_service(
        self, name: str, project: str, allow_cache: bool = False
    ) -> FeatureService:
        """
        Retrieves a feature service.

        Args:
            name: Name of feature service
            project: Feast project that this feature service belongs to
            allow_cache: Whether to allow returning this feature service from a cached registry

        Returns:
            Returns either the specified feature service, or raises an exception if
            none is found
        """
        registry = self._get_registry_proto(allow_cache=allow_cache)

        for feature_service_proto in registry.feature_services:
            if (
                feature_service_proto.spec.project == project
                and feature_service_proto.spec.name == name
            ):
                return FeatureService.from_proto(feature_service_proto)
        raise FeatureServiceNotFoundException(name, project=project)

    def get_entity(self, name: str, project: str, allow_cache: bool = False) -> Entity:
        """
        Retrieves an entity.

        Args:
            name: Name of entity
            project: Feast project that this entity belongs to
            allow_cache: Whether to allow returning this entity from a cached registry

        Returns:
            Returns either the specified entity, or raises an exception if
            none is found
        """
        registry_proto = self._get_registry_proto(allow_cache=allow_cache)
        for entity_proto in registry_proto.entities:
            if entity_proto.spec.name == name and entity_proto.spec.project == project:
                return Entity.from_proto(entity_proto)
        raise EntityNotFoundException(name, project=project)

    def apply_feature_view(
        self, feature_view: BaseFeatureView, project: str, commit: bool = True
    ):
        """
        Registers a single feature view with Feast

        Args:
            feature_view: Feature view that will be registered
            project: Feast project that this feature view belongs to
            commit: Whether the change should be persisted immediately
        """
        feature_view.ensure_valid()
        if not feature_view.created_timestamp:
            feature_view.created_timestamp = datetime.now()
        feature_view_proto = feature_view.to_proto()
        feature_view_proto.spec.project = project
        self._prepare_registry_for_changes()
        assert self.cached_registry_proto

        self._check_conflicting_feature_view_names(feature_view)
        existing_feature_views_of_same_type: RepeatedCompositeFieldContainer
        if isinstance(feature_view, FeatureView):
            existing_feature_views_of_same_type = (
                self.cached_registry_proto.feature_views
            )
        elif isinstance(feature_view, OnDemandFeatureView):
            existing_feature_views_of_same_type = (
                self.cached_registry_proto.on_demand_feature_views
            )
        elif isinstance(feature_view, RequestFeatureView):
            existing_feature_views_of_same_type = (
                self.cached_registry_proto.request_feature_views
            )
        else:
            raise ValueError(f"Unexpected feature view type: {type(feature_view)}")

        for idx, existing_feature_view_proto in enumerate(
            existing_feature_views_of_same_type
        ):
            if (
                existing_feature_view_proto.spec.name == feature_view_proto.spec.name
                and existing_feature_view_proto.spec.project == project
            ):
                if (
                    feature_view.__class__.from_proto(existing_feature_view_proto)
                    == feature_view
                ):
                    return
                else:
                    del existing_feature_views_of_same_type[idx]
                    break

        existing_feature_views_of_same_type.append(feature_view_proto)
        if commit:
            self.commit()

    def list_on_demand_feature_views(
        self, project: str, allow_cache: bool = False
    ) -> List[OnDemandFeatureView]:
        """
        Retrieve a list of on demand feature views from the registry

        Args:
            project: Filter on demand feature views based on project name
            allow_cache: Whether to allow returning on demand feature views from a cached registry

        Returns:
            List of on demand feature views
        """

        registry = self._get_registry_proto(allow_cache=allow_cache)
        on_demand_feature_views = []
        for on_demand_feature_view in registry.on_demand_feature_views:
            if on_demand_feature_view.spec.project == project:
                on_demand_feature_views.append(
                    OnDemandFeatureView.from_proto(on_demand_feature_view)
                )
        return on_demand_feature_views

    def get_on_demand_feature_view(
        self, name: str, project: str, allow_cache: bool = False
    ) -> OnDemandFeatureView:
        """
        Retrieves an on demand feature view.

        Args:
            name: Name of on demand feature view
            project: Feast project that this on demand feature  belongs to

        Returns:
            Returns either the specified on demand feature view, or raises an exception if
            none is found
        """
        registry = self._get_registry_proto(allow_cache=allow_cache)

        for on_demand_feature_view in registry.on_demand_feature_views:
            if (
                on_demand_feature_view.spec.project == project
                and on_demand_feature_view.spec.name == name
            ):
                return OnDemandFeatureView.from_proto(on_demand_feature_view)
        raise OnDemandFeatureViewNotFoundException(name, project=project)

    def apply_materialization(
        self,
        feature_view: FeatureView,
        project: str,
        start_date: datetime,
        end_date: datetime,
        commit: bool = True,
    ):
        """
        Updates materialization intervals tracked for a single feature view in Feast

        Args:
            feature_view: Feature view that will be updated with an additional materialization interval tracked
            project: Feast project that this feature view belongs to
            start_date (datetime): Start date of the materialization interval to track
            end_date (datetime): End date of the materialization interval to track
            commit: Whether the change should be persisted immediately
        """
        self._prepare_registry_for_changes()
        assert self.cached_registry_proto

        for idx, existing_feature_view_proto in enumerate(
            self.cached_registry_proto.feature_views
        ):
            if (
                existing_feature_view_proto.spec.name == feature_view.name
                and existing_feature_view_proto.spec.project == project
            ):
                existing_feature_view = FeatureView.from_proto(
                    existing_feature_view_proto
                )
                existing_feature_view.materialization_intervals.append(
                    (start_date, end_date)
                )
                feature_view_proto = existing_feature_view.to_proto()
                feature_view_proto.spec.project = project
                del self.cached_registry_proto.feature_views[idx]
                self.cached_registry_proto.feature_views.append(feature_view_proto)
                if commit:
                    self.commit()
                return

        raise FeatureViewNotFoundException(feature_view.name, project)

    def list_feature_views(
        self, project: str, allow_cache: bool = False
    ) -> List[FeatureView]:
        """
        Retrieve a list of feature views from the registry

        Args:
            allow_cache: Allow returning feature views from the cached registry
            project: Filter feature views based on project name

        Returns:
            List of feature views
        """
        registry_proto = self._get_registry_proto(allow_cache=allow_cache)
        feature_views: List[FeatureView] = []
        for feature_view_proto in registry_proto.feature_views:
            if feature_view_proto.spec.project == project:
                feature_views.append(FeatureView.from_proto(feature_view_proto))
        return feature_views

    def list_request_feature_views(
        self, project: str, allow_cache: bool = False
    ) -> List[RequestFeatureView]:
        """
        Retrieve a list of request feature views from the registry

        Args:
            allow_cache: Allow returning feature views from the cached registry
            project: Filter feature views based on project name

        Returns:
            List of feature views
        """
        registry_proto = self._get_registry_proto(allow_cache=allow_cache)
        feature_views: List[RequestFeatureView] = []
        for request_feature_view_proto in registry_proto.request_feature_views:
            if request_feature_view_proto.spec.project == project:
                feature_views.append(
                    RequestFeatureView.from_proto(request_feature_view_proto)
                )
        return feature_views

    def get_feature_view(
        self, name: str, project: str, allow_cache: bool = False
    ) -> FeatureView:
        """
        Retrieves a feature view.

        Args:
            name: Name of feature view
            project: Feast project that this feature view belongs to
            allow_cache: Allow returning feature view from the cached registry

        Returns:
            Returns either the specified feature view, or raises an exception if
            none is found
        """
        registry_proto = self._get_registry_proto(allow_cache=allow_cache)
        for feature_view_proto in registry_proto.feature_views:
            if (
                feature_view_proto.spec.name == name
                and feature_view_proto.spec.project == project
            ):
                return FeatureView.from_proto(feature_view_proto)
        raise FeatureViewNotFoundException(name, project)

    def delete_feature_service(self, name: str, project: str, commit: bool = True):
        """
        Deletes a feature service or raises an exception if not found.

        Args:
            name: Name of feature service
            project: Feast project that this feature service belongs to
            commit: Whether the change should be persisted immediately
        """
        self._prepare_registry_for_changes()
        assert self.cached_registry_proto

        for idx, feature_service_proto in enumerate(
            self.cached_registry_proto.feature_services
        ):
            if (
                feature_service_proto.spec.name == name
                and feature_service_proto.spec.project == project
            ):
                del self.cached_registry_proto.feature_services[idx]
                if commit:
                    self.commit()
                return
        raise FeatureServiceNotFoundException(name, project)

    def delete_feature_view(self, name: str, project: str, commit: bool = True):
        """
        Deletes a feature view or raises an exception if not found.

        Args:
            name: Name of feature view
            project: Feast project that this feature view belongs to
            commit: Whether the change should be persisted immediately
        """
        self._prepare_registry_for_changes()
        assert self.cached_registry_proto

        for idx, existing_feature_view_proto in enumerate(
            self.cached_registry_proto.feature_views
        ):
            if (
                existing_feature_view_proto.spec.name == name
                and existing_feature_view_proto.spec.project == project
            ):
                del self.cached_registry_proto.feature_views[idx]
                if commit:
                    self.commit()
                return

        for idx, existing_request_feature_view_proto in enumerate(
            self.cached_registry_proto.request_feature_views
        ):
            if (
                existing_request_feature_view_proto.spec.name == name
                and existing_request_feature_view_proto.spec.project == project
            ):
                del self.cached_registry_proto.request_feature_views[idx]
                if commit:
                    self.commit()
                return

        raise FeatureViewNotFoundException(name, project)

    def delete_entity(self, name: str, project: str, commit: bool = True):
        """
        Deletes an entity or raises an exception if not found.

        Args:
            name: Name of entity
            project: Feast project that this entity belongs to
            commit: Whether the change should be persisted immediately
        """
        self._prepare_registry_for_changes()
        assert self.cached_registry_proto

        for idx, existing_entity_proto in enumerate(
            self.cached_registry_proto.entities
        ):
            if (
                existing_entity_proto.spec.name == name
                and existing_entity_proto.spec.project == project
            ):
                del self.cached_registry_proto.entities[idx]
                if commit:
                    self.commit()
                return

        raise EntityNotFoundException(name, project)

    def commit(self):
        """Commits the state of the registry cache to the remote registry store."""
        if self.cached_registry_proto:
            self._registry_store.update_registry_proto(self.cached_registry_proto)

    def refresh(self):
        """Refreshes the state of the registry cache by fetching the registry state from the remote registry store."""
        self._get_registry_proto(allow_cache=False)

    def teardown(self):
        """Tears down (removes) the registry."""
        self._registry_store.teardown()

    def to_dict(self, project: str) -> Dict[str, List[Any]]:
        """Returns a dictionary representation of the registry contents for the specified project.

        For each list in the dictionary, the elements are sorted by name, so this
        method can be used to compare two registries.

        Args:
            project: Feast project to convert to a dict
        """
        registry_dict = defaultdict(list)

        for entity in sorted(
            self.list_entities(project=project), key=lambda entity: entity.name
        ):
            registry_dict["entities"].append(MessageToDict(entity.to_proto()))
        for feature_view in sorted(
            self.list_feature_views(project=project),
            key=lambda feature_view: feature_view.name,
        ):
            registry_dict["featureViews"].append(MessageToDict(feature_view.to_proto()))
        for feature_service in sorted(
            self.list_feature_services(project=project),
            key=lambda feature_service: feature_service.name,
        ):
            registry_dict["featureServices"].append(
                MessageToDict(feature_service.to_proto())
            )
        for on_demand_feature_view in sorted(
            self.list_on_demand_feature_views(project=project),
            key=lambda on_demand_feature_view: on_demand_feature_view.name,
        ):
            registry_dict["onDemandFeatureViews"].append(
                MessageToDict(on_demand_feature_view.to_proto())
            )
        for request_feature_view in sorted(
            self.list_request_feature_views(project=project),
            key=lambda request_feature_view: request_feature_view.name,
        ):
            registry_dict["requestFeatureViews"].append(
                MessageToDict(request_feature_view.to_proto())
            )
        return registry_dict

    def _prepare_registry_for_changes(self):
        """Prepares the Registry for changes by refreshing the cache if necessary."""
        try:
            self._get_registry_proto(allow_cache=True)
        except FileNotFoundError:
            registry_proto = RegistryProto()
            registry_proto.registry_schema_version = REGISTRY_SCHEMA_VERSION
            self.cached_registry_proto = registry_proto
            self.cached_registry_proto_created = datetime.now()
        return self.cached_registry_proto

    def _get_registry_proto(self, allow_cache: bool = False) -> RegistryProto:
        """Returns the cached or remote registry state

        Args:
            allow_cache: Whether to allow the use of the registry cache when fetching the RegistryProto

        Returns: Returns a RegistryProto object which represents the state of the registry
        """
        with self._refresh_lock:
            expired = (
                self.cached_registry_proto is None
                or self.cached_registry_proto_created is None
            ) or (
                self.cached_registry_proto_ttl.total_seconds()
                > 0  # 0 ttl means infinity
                and (
                    datetime.now()
                    > (
                        self.cached_registry_proto_created
                        + self.cached_registry_proto_ttl
                    )
                )
            )

            if allow_cache and not expired:
                assert isinstance(self.cached_registry_proto, RegistryProto)
                return self.cached_registry_proto

            registry_proto = self._registry_store.get_registry_proto()
            self.cached_registry_proto = registry_proto
            self.cached_registry_proto_created = datetime.now()

            return registry_proto

    def _check_conflicting_feature_view_names(self, feature_view: BaseFeatureView):
        name_to_fv_protos = self._existing_feature_view_names_to_fvs()
        if feature_view.name in name_to_fv_protos:
            if not isinstance(
                name_to_fv_protos.get(feature_view.name), feature_view.proto_class
            ):
                raise ConflictingFeatureViewNames(feature_view.name)

    def _existing_feature_view_names_to_fvs(self) -> Dict[str, Message]:
        assert self.cached_registry_proto
        odfvs = {
            fv.spec.name: fv
            for fv in self.cached_registry_proto.on_demand_feature_views
        }
        fvs = {fv.spec.name: fv for fv in self.cached_registry_proto.feature_views}
        request_fvs = {
            fv.spec.name: fv for fv in self.cached_registry_proto.request_feature_views
        }
        return {**odfvs, **fvs, **request_fvs}
