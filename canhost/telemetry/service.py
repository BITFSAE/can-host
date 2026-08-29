"""Independent MQTT subscription lifetime for the telemetry monitor page."""

from __future__ import annotations

from collections import deque
from datetime import datetime
import threading
import time
from typing import Any, Callable
import uuid

from .protocol import decode_telemetry_payload


DEFAULT_HOST = "bitfsae.com"
DEFAULT_PORT = 1883
DEFAULT_TOPIC = "fsae/telemetry"
MAX_HISTORY = 120


def _reason_value(reason_code: Any) -> int:
    value = getattr(reason_code, "value", reason_code)
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


class TelemetryService:
    """Subscribe, decode and retain a bounded telemetry fault history."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        decoder: Callable[[bytes], dict[str, Any]] = decode_telemetry_payload,
    ) -> None:
        self._clock = clock
        self._wall_clock = wall_clock
        self._decoder = decoder
        self._lock = threading.RLock()
        self._client: Any = None
        self._generation = 0
        self._connection: dict[str, Any] = {
            "state": "disconnected",
            "connected": False,
            "host": DEFAULT_HOST,
            "port": DEFAULT_PORT,
            "topic": DEFAULT_TOPIC,
            "username": "",
            "tls": False,
            "error": None,
        }
        self._latest: dict[str, Any] | None = None
        self._last_message_monotonic: float | None = None
        self._rx_count = 0
        self._rx_bytes = 0
        self._parse_error_count = 0
        self._last_parse_error: str | None = None
        self._previous_fault_code: int | None = None
        self._fault_history: deque[dict[str, Any]] = deque(maxlen=MAX_HISTORY)

    def connect(self, config: dict[str, Any]) -> dict[str, Any]:
        host = str(config.get("host") or DEFAULT_HOST).strip()
        topic = str(config.get("topic") or DEFAULT_TOPIC).strip()
        username = str(config.get("username") or "").strip()
        password = str(config.get("password") or "")
        tls = bool(config.get("tls", False))
        try:
            port = int(config.get("port") or (8883 if tls else DEFAULT_PORT))
        except (TypeError, ValueError):
            return {"ok": False, "error": "MQTT 端口必须是 1..65535 的整数"}
        if not host:
            return {"ok": False, "error": "MQTT Broker 地址不能为空"}
        if not topic or "#" in topic or "+" in topic:
            return {"ok": False, "error": "遥测 Topic 必须是明确主题，不能使用通配符"}
        if not 1 <= port <= 65535:
            return {"ok": False, "error": "MQTT 端口必须在 1..65535"}
        if bool(username) != bool(password):
            return {"ok": False, "error": "MQTT 用户名和密码必须同时填写"}

        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            return {"ok": False, "error": "缺少 paho-mqtt，请重新安装上位机依赖"}
        try:
            from . import fsae_telemetry_pb2  # noqa: F401
        except Exception as exc:
            return {"ok": False, "error": f"Protobuf 运行库缺失或版本不兼容：{exc}"}

        self.disconnect()
        with self._lock:
            self._generation += 1
            generation = self._generation
            self._connection = {
                "state": "connecting",
                "connected": False,
                "host": host,
                "port": port,
                "topic": topic,
                "username": username,
                "tls": tls,
                "error": None,
            }
            self._latest = None
            self._last_message_monotonic = None
            self._previous_fault_code = None
            self._fault_history.clear()
            self._rx_count = 0
            self._rx_bytes = 0
            self._parse_error_count = 0
            self._last_parse_error = None

        try:
            client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                client_id=f"can-host-{uuid.uuid4().hex[:10]}",
                clean_session=True,
                protocol=mqtt.MQTTv311,
            )
            if username:
                client.username_pw_set(username, password)
            if tls:
                client.tls_set()
        except Exception as exc:
            with self._lock:
                self._connection.update(
                    state="error", connected=False, error=f"MQTT 客户端初始化失败：{exc}")
            return {"ok": False, "error": f"MQTT 客户端初始化失败：{exc}"}

        client.on_connect = lambda c, u, f, rc, p=None: self._on_connect(
            generation, c, rc, topic)
        client.on_connect_fail = lambda c, u: self._on_connect_fail(generation)
        client.on_disconnect = lambda c, u, df, rc, p=None: self._on_disconnect(
            generation, rc)
        client.on_message = lambda c, u, message: self._on_message(
            generation, message.topic, bytes(message.payload))

        with self._lock:
            self._client = client
        try:
            client.connect_async(host, port, keepalive=30)
            client.loop_start()
        except Exception as exc:
            with self._lock:
                if generation == self._generation:
                    self._client = None
                    self._connection.update(
                        state="error", connected=False, error=f"MQTT 启动失败：{exc}")
            return {"ok": False, "error": f"MQTT 启动失败：{exc}"}
        return {"ok": True, "state": "connecting", "message": f"正在连接 {host}:{port}"}

    def disconnect(self) -> dict[str, Any]:
        with self._lock:
            self._generation += 1
            client = self._client
            self._client = None
            self._connection["state"] = "disconnected"
            self._connection["connected"] = False
            self._connection["error"] = None
        if client is not None:
            try:
                client.disconnect()
            except Exception:
                pass
            try:
                client.loop_stop()
            except Exception:
                pass
        return {"ok": True}

    def _on_connect(self, generation: int, client: Any, reason_code: Any, topic: str) -> None:
        code = _reason_value(reason_code)
        with self._lock:
            if generation != self._generation or client is not self._client:
                return
            if code != 0:
                self._connection.update(
                    state="error", connected=False,
                    error=f"MQTT 连接被拒绝（{reason_code}）",
                )
                return
        try:
            result, _mid = client.subscribe(topic, qos=0)
        except Exception as exc:
            with self._lock:
                if generation == self._generation and client is self._client:
                    self._connection.update(
                        state="error", connected=False,
                        error=f"订阅 {topic} 失败：{exc}",
                    )
            return
        with self._lock:
            if generation != self._generation or client is not self._client:
                return
            if result != 0:
                self._connection.update(
                    state="error", connected=False,
                    error=f"订阅 {topic} 失败（代码 {result}）",
                )
            else:
                self._connection.update(state="subscribed", connected=True, error=None)

    def _on_connect_fail(self, generation: int) -> None:
        with self._lock:
            if generation != self._generation:
                return
            self._connection.update(
                state="error", connected=False,
                error="无法连接 MQTT Broker，请检查网络、地址和端口",
            )

    def _on_disconnect(self, generation: int, reason_code: Any) -> None:
        with self._lock:
            if generation != self._generation:
                return
            code = _reason_value(reason_code)
            self._connection["connected"] = False
            if code == 0:
                self._connection.update(state="disconnected", error=None)
            else:
                self._connection.update(
                    state="reconnecting",
                    error=f"MQTT 连接中断（{reason_code}），正在重连",
                )

    def _on_message(self, generation: int, topic: str, payload: bytes) -> None:
        try:
            decoded = self._decoder(payload)
        except Exception as exc:
            with self._lock:
                if generation != self._generation:
                    return
                self._parse_error_count += 1
                self._last_parse_error = str(exc)
            return

        now_mono = self._clock()
        received_at = self._wall_clock()
        decoded["received_at"] = datetime.fromtimestamp(received_at).strftime(
            "%Y-%m-%d %H:%M:%S.%f")[:-3]
        decoded["topic"] = topic
        fault_code = int(decoded.get("fault", {}).get("code", 0))
        fault_valid = bool(decoded.get("fault", {}).get("valid", False))
        with self._lock:
            if generation != self._generation:
                return
            if fault_valid:
                previous = self._previous_fault_code
                if previous is None:
                    if fault_code != 0:
                        self._append_fault_event(decoded, 0, fault_code)
                elif previous != fault_code:
                    self._append_fault_event(decoded, previous, fault_code)
                self._previous_fault_code = fault_code
            self._latest = decoded
            self._last_message_monotonic = now_mono
            self._rx_count += 1
            self._rx_bytes += len(payload)
            self._connection.update(state="subscribed", connected=True, error=None)

    def _append_fault_event(self, decoded: dict[str, Any], previous: int, current: int) -> None:
        added = current & ~previous
        cleared = previous & ~current
        names = {item["bit"]: item["name"] for item in decoded["fault"]["active"]}
        # The current active list cannot name just-cleared bits, so use the same
        # canonical BMS table for both directions.
        from ..bms.protocol import ALARM_NAMES
        self._fault_history.appendleft({
            "time": decoded["received_at"],
            "previous": f"0x{previous:08X}",
            "code": f"0x{current:08X}",
            "added": [names.get(bit, ALARM_NAMES[bit]) for bit in range(32) if added & (1 << bit)],
            "cleared": [ALARM_NAMES[bit] for bit in range(32) if cleared & (1 << bit)],
        })

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            connection = dict(self._connection)
            latest = dict(self._latest) if self._latest is not None else None
            age = (None if self._last_message_monotonic is None else
                   max(0.0, self._clock() - self._last_message_monotonic))
            return {
                "connection": connection,
                "latest": latest,
                "last_message_age": age,
                "rx_count": self._rx_count,
                "rx_bytes": self._rx_bytes,
                "parse_error_count": self._parse_error_count,
                "last_parse_error": self._last_parse_error,
                "fault_history": list(self._fault_history),
            }


__all__ = ["DEFAULT_HOST", "DEFAULT_PORT", "DEFAULT_TOPIC", "TelemetryService"]
