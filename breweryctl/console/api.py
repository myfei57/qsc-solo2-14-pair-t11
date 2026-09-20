"""JSON API 路由。"""

from __future__ import annotations

from typing import Any, Callable

from ..core.errors import BreweryError, NotFoundError, ValidationError
from ..core.validators import require_int
from ..service.registry import ComponentRegistry
from . import serializers
from .pages import page_catalog

Handler = Callable[[dict[str, str], dict[str, list[str]], dict[str, Any]], dict[str, Any]]

ROUTES: tuple[tuple[str, str], ...] = (
    ("GET", "/api/health"),
    ("GET", "/api/state"),
    ("GET", "/api/pages"),
    ("GET", "/api/recipes"),
    ("POST", "/api/recipes"),
    ("GET", "/api/recipes/{recipe_id}"),
    ("POST", "/api/recipes/{recipe_id}/publish"),
    ("POST", "/api/recipes/{recipe_id}/revise"),
    ("POST", "/api/recipes/{recipe_id}/rollback"),
    ("POST", "/api/recipes/{recipe_id}/archive"),
    ("GET", "/api/batches"),
    ("POST", "/api/batches"),
    ("GET", "/api/batches/{batch_id}"),
    ("GET", "/api/batches/{batch_id}/audit"),
    ("POST", "/api/batches/{batch_id}/water"),
    ("POST", "/api/batches/{batch_id}/charge"),
    ("POST", "/api/batches/{batch_id}/heat"),
    ("POST", "/api/batches/{batch_id}/rest"),
    ("POST", "/api/batches/{batch_id}/filter"),
    ("POST", "/api/batches/{batch_id}/ignite"),
    ("POST", "/api/batches/{batch_id}/boil"),
    ("POST", "/api/batches/{batch_id}/hop"),
    ("POST", "/api/batches/{batch_id}/whirlpool"),
    ("POST", "/api/batches/{batch_id}/cool"),
    ("POST", "/api/batches/{batch_id}/cooled"),
    ("POST", "/api/batches/{batch_id}/transfer"),
    ("POST", "/api/batches/{batch_id}/pitch"),
    ("POST", "/api/batches/{batch_id}/mature"),
    ("POST", "/api/batches/{batch_id}/complete"),
    ("POST", "/api/batches/{batch_id}/abort"),
    ("GET", "/api/control/tanks"),
    ("GET", "/api/control/tanks/{tank_id}"),
    ("POST", "/api/control/tanks/{tank_id}/pressure"),
    ("POST", "/api/control/tanks/{tank_id}/relieve"),
    ("POST", "/api/control/tanks/{tank_id}/latch/reset"),
    ("POST", "/api/control/tanks/{tank_id}/valves"),
    ("POST", "/api/control/temperature"),
    ("GET", "/api/control/temperature/{batch_id}/readiness"),
    ("GET", "/api/control/temperature/{batch_id}/progress"),
    ("GET", "/api/telemetry/probes/{probe_id}/health"),
    ("GET", "/api/telemetry/probes/{probe_id}/report"),
    ("POST", "/api/telemetry/probes/{probe_id}/calibrate"),
    ("GET", "/api/telemetry/batches/{batch_id}/trend"),
    ("GET", "/api/telemetry/batches/{batch_id}/latest"),
    ("POST", "/api/telemetry/readings"),
    ("POST", "/api/telemetry/readings/batch"),
    ("GET", "/api/maintenance/tanks/{tank_id}/certificate"),
    ("POST", "/api/maintenance/tanks/{tank_id}/clean"),
    ("POST", "/api/maintenance/tanks/{tank_id}/empty"),
    ("POST", "/api/maintenance/cycles/{cycle_id}/advance"),
    ("POST", "/api/maintenance/cycles/{cycle_id}/finish"),
    ("POST", "/api/maintenance/circuits/{circuit_id}/flush"),
    ("GET", "/api/alarms"),
    ("POST", "/api/alarms/{alarm_id}/ack"),
    ("POST", "/api/alarms/{alarm_id}/resolve"),
    ("GET", "/api/audit"),
    ("GET", "/api/outbound/status"),
    ("GET", "/api/outbound/events"),
    ("POST", "/api/outbound/flush"),
    ("POST", "/api/outbound/reconcile"),
    ("POST", "/api/outbound/events/{event_id}/revive"),
)


class ApiRouter:
    """把 HTTP 请求映射到服务层调用。"""

    def __init__(self, registry: ComponentRegistry) -> None:
        self.registry = registry
        self._routes = {
            (method, pattern): getattr(self, "_handle_" + _handler_name(method, pattern))
            for method, pattern in ROUTES
        }

    def handle(
        self,
        method: str,
        path: str,
        query: dict[str, list[str]],
        body: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        """执行路由并返回状态码与响应体。"""

        try:
            handler, params = self._resolve(method.upper(), path)
            if handler is None:
                raise NotFoundError("接口不存在", method=method.upper(), path=path)
            payload = handler(params, query, body)
            return 200, payload
        except BreweryError as exc:
            return exc.http_status, serializers.error_payload(exc)
        except Exception as exc:  # noqa: BLE001 - 统一兜底为 500 响应
            return 500, serializers.unexpected_payload(exc)

    def route_table(self) -> list[dict[str, str]]:
        """返回可公开的路由清单。"""

        return [{"method": method, "path": path} for method, path in ROUTES]

    def _resolve(
        self, method: str, path: str
    ) -> tuple[Handler | None, dict[str, str]]:
        parts = [segment for segment in path.strip("/").split("/") if segment]
        for (route_method, pattern), handler in self._routes.items():
            if route_method != method:
                continue
            params = _match(pattern, parts)
            if params is not None:
                return handler, params
        return None, {}

    def _handle_GET_api_health(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        stats = self.registry.store.stats()
        return {
            "status": "ok",
            "booted_at": self.registry.store.meta("booted_at"),
            "sequence": stats["sequence"],
            "writes": stats["writes"],
            "data_dir": stats["data_dir"],
        }

    def _handle_GET_api_state(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        overview = self.registry.state_overview()
        overview["banner"] = serializers.page_banner(overview)
        return overview

    def _handle_GET_api_pages(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        return {"pages": page_catalog(), "routes": self.route_table()}

    def _handle_GET_api_recipes(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        status = _first(query, "status")
        items = self.registry.recipes.list(status=status)
        return {"recipes": [serializers.recipe_summary(item) for item in items]}

    def _handle_POST_api_recipes(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        recipe = self.registry.recipes.define(
            name=body.get("name"),
            style=body.get("style"),
            brewery_id=body.get("brewery_id"),
            volume_l=body.get("volume_l"),
            boil_minutes=body.get("boil_minutes"),
            og_target=body.get("og_target"),
            fg_target=body.get("fg_target"),
            ibu_target=body.get("ibu_target"),
            mash_steps=_require_list(body, "mash_steps"),
            hop_schedule=_require_list(body, "hop_schedule"),
        )
        return {"recipe": serializers.recipe_summary(recipe)}

    def _handle_GET_api_recipes_recipe_id(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        recipe_id = params["recipe_id"]
        document = self.registry.recipes.get(recipe_id)
        return {
            "recipe": document,
            "history": self.registry.recipes.history(recipe_id),
        }

    def _handle_POST_api_recipes_recipe_id_publish(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        document = self.registry.recipes.publish(params["recipe_id"])
        return {"recipe": serializers.recipe_summary(document)}

    def _handle_POST_api_recipes_recipe_id_revise(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        allowed = {"name", "style", "volume_l", "boil_minutes", "og_target", "fg_target", "ibu_target"}
        patch = {key: value for key, value in body.items() if key in allowed}
        if not patch:
            raise ValidationError("修订请求没有任何可更新字段", allowed=sorted(allowed))
        document = self.registry.recipes.revise(params["recipe_id"], **patch)
        return {"recipe": serializers.recipe_summary(document)}

    def _handle_POST_api_recipes_recipe_id_rollback(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        version = require_int(body.get("version"), field="version", minimum=1)
        document = self.registry.recipes.rollback(params["recipe_id"], version)
        return {"recipe": serializers.recipe_summary(document)}

    def _handle_POST_api_recipes_recipe_id_archive(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        document = self.registry.recipes.archive(params["recipe_id"])
        return {"recipe": serializers.recipe_summary(document)}

    def _handle_GET_api_batches(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        items = self.registry.brewing.list_batches(stage=_first(query, "stage"))
        return {"batches": serializers.batch_collection(items)}

    def _handle_POST_api_batches(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        view = self.registry.brewing.create_batch(
            recipe_id=body.get("recipe_id"),
            volume_l=body.get("volume_l"),
            actor=body.get("actor"),
            priority=str(body.get("priority", "normal")),
            notes=str(body.get("notes", "")),
        )
        return view

    def _handle_GET_api_batches_batch_id(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        return self.registry.brewing.status(params["batch_id"])

    def _handle_GET_api_batches_batch_id_audit(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        return self.registry.brewing.audit_history(params["batch_id"])

    def _handle_POST_api_batches_batch_id_water(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        return self.registry.brewing.confirm_water(
            params["batch_id"], body.get("temp_c"), body.get("probe_id"), body.get("actor")
        )

    def _handle_POST_api_batches_batch_id_charge(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        return self.registry.brewing.charge_mash(
            params["batch_id"], body.get("grain_kg"), body.get("actor")
        )

    def _handle_POST_api_batches_batch_id_heat(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        return self.registry.brewing.heat_mash(
            params["batch_id"], body.get("setpoint_c"), body.get("actor")
        )

    def _handle_POST_api_batches_batch_id_rest(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        return self.registry.brewing.rest_mash(
            params["batch_id"], body.get("actual_temp_c"), body.get("actor")
        )

    def _handle_POST_api_batches_batch_id_filter(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        return self.registry.brewing.filter_mash(
            params["batch_id"],
            body.get("gravity_plato"),
            body.get("volume_l"),
            body.get("ph"),
            body.get("actor"),
        )

    def _handle_POST_api_batches_batch_id_ignite(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        return self.registry.brewing.ignite_boil(params["batch_id"], body.get("actor"))

    def _handle_POST_api_batches_batch_id_boil(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        return self.registry.brewing.boil_rolling(params["batch_id"], body.get("actor"))

    def _handle_POST_api_batches_batch_id_hop(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        return self.registry.brewing.add_hop(
            params["batch_id"], body.get("position"), body.get("minute"), body.get("actor")
        )

    def _handle_POST_api_batches_batch_id_whirlpool(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        return self.registry.brewing.whirlpool(
            params["batch_id"], body.get("minute"), body.get("actor")
        )

    def _handle_POST_api_batches_batch_id_cool(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        return self.registry.brewing.cool_down(
            params["batch_id"], body.get("target_c"), body.get("actor")
        )

    def _handle_POST_api_batches_batch_id_cooled(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        return self.registry.brewing.mark_cooled(
            params["batch_id"], body.get("value_c"), body.get("actor")
        )

    def _handle_POST_api_batches_batch_id_transfer(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        return self.registry.brewing.transfer_to_tank(
            params["batch_id"], body.get("tank_id"), body.get("actor")
        )

    def _handle_POST_api_batches_batch_id_pitch(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        return self.registry.brewing.pitch_yeast(
            params["batch_id"],
            body.get("tank_id"),
            body.get("temp_c"),
            body.get("volume_l"),
            body.get("actor"),
        )

    def _handle_POST_api_batches_batch_id_mature(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        return self.registry.brewing.mature_batch(
            params["batch_id"], body.get("tank_id"), body.get("days"), body.get("actor")
        )

    def _handle_POST_api_batches_batch_id_complete(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        return self.registry.brewing.complete_batch(params["batch_id"], body.get("actor"))

    def _handle_POST_api_batches_batch_id_abort(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        return self.registry.brewing.abort_batch(
            params["batch_id"], body.get("reason"), body.get("actor")
        )

    def _handle_GET_api_control_tanks(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        tanks = self.registry.tanks.list_tanks(_first(query, "brewery_id"))
        snapshots = [self.registry.control.tank_snapshot(str(item["id"])) for item in tanks]
        return {"tanks": [serializers.tank_summary(item) for item in tanks], "snapshots": snapshots}

    def _handle_GET_api_control_tanks_tank_id(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        return self.registry.control.tank_snapshot(params["tank_id"])

    def _handle_POST_api_control_tanks_tank_id_pressure(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        return self.registry.control.set_pressure(
            params["tank_id"], body.get("pressure_bar"), body.get("actor")
        )

    def _handle_POST_api_control_tanks_tank_id_relieve(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        return self.registry.control.relieve_pressure(
            params["tank_id"], body.get("target_bar"), body.get("actor")
        )

    def _handle_POST_api_control_tanks_tank_id_latch_reset(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        return self.registry.control.reset_latch(params["tank_id"], body.get("operator"))

    def _handle_POST_api_control_tanks_tank_id_valves(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        return self.registry.control.operate_valve(
            params["tank_id"],
            body.get("purpose"),
            body.get("state"),
            body.get("operator"),
        )

    def _handle_POST_api_control_temperature(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        return self.registry.control.set_temperature(
            body.get("batch_id"),
            body.get("target_c"),
            body.get("actor"),
            cooling=bool(body.get("cooling", False)),
        )

    def _handle_GET_api_control_temperature_batch_id_readiness(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        return self.registry.control.pitch_readiness(params["batch_id"])

    def _handle_GET_api_control_temperature_batch_id_progress(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        current = _required_number(_first(query, "current_c"), field="current_c")
        return self.registry.control.cooling_progress(params["batch_id"], current)

    def _handle_GET_api_telemetry_probes_probe_id_health(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        return self.registry.telemetry.probe_health(params["probe_id"])

    def _handle_GET_api_telemetry_probes_probe_id_report(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        window = _optional_int(_first(query, "window"), field="window", default=5, minimum=1, maximum=100)
        return self.registry.telemetry.probe_report(params["probe_id"], window=window)

    def _handle_POST_api_telemetry_probes_probe_id_calibrate(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        return self.registry.telemetry.calibrate(
            params["probe_id"], body.get("baseline_c"), body.get("actor")
        )

    def _handle_GET_api_telemetry_batches_batch_id_trend(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        limit = _optional_int(_first(query, "limit"), field="limit", default=50, minimum=1, maximum=500)
        return self.registry.telemetry.batch_trend(params["batch_id"], limit=limit)

    def _handle_GET_api_telemetry_batches_batch_id_latest(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        probe_id = _first(query, "probe_id")
        if not probe_id:
            raise ValidationError("缺少 probe_id 查询参数", field="probe_id")
        reading = self.registry.telemetry.require_recent(probe_id, params["batch_id"])
        return {"reading": serializers.telemetry_view(reading)}

    def _handle_POST_api_telemetry_readings(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        reading = self.registry.telemetry.ingest(
            body.get("probe_id"),
            body.get("value_c"),
            body.get("batch_id"),
            body.get("actor"),
        )
        return {"reading": serializers.telemetry_view(reading)}

    def _handle_POST_api_telemetry_readings_batch(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        samples = _require_list(body, "samples")
        return self.registry.telemetry.ingest_many(samples, body.get("actor"))

    def _handle_GET_api_maintenance_tanks_tank_id_certificate(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        return self.registry.maintenance.certificate_status(params["tank_id"])

    def _handle_POST_api_maintenance_tanks_tank_id_clean(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        cycle = self.registry.maintenance.start_clean(params["tank_id"], body.get("operator"))
        return {"cycle": cycle}

    def _handle_POST_api_maintenance_tanks_tank_id_empty(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        tank = self.registry.maintenance.empty_tank(params["tank_id"], body.get("operator"))
        return {"tank": serializers.tank_summary(tank)}

    def _handle_POST_api_maintenance_cycles_cycle_id_advance(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        cycle = self.registry.maintenance.advance_clean(params["cycle_id"], body.get("operator"))
        return {"cycle": cycle}

    def _handle_POST_api_maintenance_cycles_cycle_id_finish(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        return self.registry.maintenance.finish_clean(params["cycle_id"], body.get("operator"))

    def _handle_POST_api_maintenance_circuits_circuit_id_flush(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        return self.registry.maintenance.flush_circuit(
            params["circuit_id"], body.get("flow_m3h"), body.get("operator")
        )

    def _handle_GET_api_alarms(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        items = self.registry.alarms.list_alarms(
            status=_first(query, "status"),
            brewery_id=_first(query, "brewery_id"),
            severity=_first(query, "severity"),
        )
        return {
            "alarms": serializers.alarm_collection(items),
            "summary": self.registry.alarms.summary(_first(query, "brewery_id")),
        }

    def _handle_POST_api_alarms_alarm_id_ack(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        alarm = self.registry.alarms.acknowledge(params["alarm_id"], body.get("operator"))
        return {"alarm": serializers.alarm_view(alarm)}

    def _handle_POST_api_alarms_alarm_id_resolve(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        alarm = self.registry.alarms.resolve(
            params["alarm_id"], body.get("operator"), body.get("note")
        )
        return {"alarm": serializers.alarm_view(alarm)}

    def _handle_GET_api_audit(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        batch_id = _first(query, "batch_id")
        limit = _optional_int(_first(query, "limit"), field="limit", default=100, minimum=1, maximum=1000)
        entries = self.registry.audit.history(batch_id=batch_id, limit=limit)
        return {"entries": entries, "count": len(entries)}

    def _handle_GET_api_outbound_status(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        return self.registry.outbound_overview()

    def _handle_GET_api_outbound_events(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        status = _first(query, "status")
        batch_id = _first(query, "batch_id")
        limit = _optional_int(_first(query, "limit"), field="limit", default=200, minimum=1, maximum=2000)
        events = self.registry.outbox.list_events(
            status=status, batch_id=batch_id, limit=limit
        )
        return {
            "events": events,
            "count": len(events),
            "stats": self.registry.outbox.stats(),
        }

    def _handle_POST_api_outbound_flush(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        return self.registry.flush_outbox()

    def _handle_POST_api_outbound_reconcile(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        batch_id = body.get("batch_id") if isinstance(body.get("batch_id"), str) else None
        client = self.registry.reconcile_client
        if client is None:
            report = self.registry.reconciler.local_check(batch_id=batch_id)
            return {"scope": "local", "report": report.as_dict()}
        report = self.registry.reconciler.remote_check(client, batch_id=batch_id)
        requeued = 0
        if report.missing_remote:
            requeued = self.registry.reconciler.requeue_missing(report)
            self.registry.relay.kick() if self.registry.relay else None
        return {"scope": "remote", "requeued": requeued, "report": report.as_dict()}

    def _handle_POST_api_outbound_events_event_id_revive(
        self, params: dict[str, str], query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        event = self.registry.outbox.revive(params["event_id"])
        if self.registry.relay is not None:
            self.registry.relay.kick()
        return {"event": event}


def _handler_name(method: str, pattern: str) -> str:
    return (method + pattern).replace("/", "_").replace("-", "_").replace("{", "").replace("}", "")


def _match(pattern: str, parts: list[str]) -> dict[str, str] | None:
    pattern_parts = [segment for segment in pattern.strip("/").split("/") if segment]
    if len(pattern_parts) != len(parts):
        return None
    params: dict[str, str] = {}
    for expected, actual in zip(pattern_parts, parts):
        if expected.startswith("{") and expected.endswith("}"):
            params[expected[1:-1]] = actual
        elif expected != actual:
            return None
    return params


def _first(query: dict[str, list[str]], name: str) -> str | None:
    values = query.get(name)
    if not values:
        return None
    return values[0]


def _optional_int(
    raw: str | None,
    field: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValidationError(f"{field} 必须是整数", field=field, value=raw) from exc
    return require_int(value, field=field, minimum=minimum, maximum=maximum)


def _require_list(body: dict[str, Any], field: str) -> list[Any]:
    value = body.get(field)
    if not isinstance(value, list):
        raise ValidationError(f"{field} 必须是数组", field=field)
    return value


def _required_number(raw: str | None, field: str) -> float:
    if raw is None:
        raise ValidationError(f"缺少 {field} 查询参数", field=field)
    try:
        return float(raw)
    except ValueError as exc:
        raise ValidationError(f"{field} 必须是数字", field=field, value=raw) from exc
