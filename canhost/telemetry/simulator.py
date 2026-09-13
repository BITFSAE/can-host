"""Local vehicle telemetry publisher used by the engineering-tool page.

The protobuf model is derived from the two identical ``local_sim2.py`` copies
kept beside CANRS485_G473 and CAN2RS485.  Physical CAN output deliberately
reuses :class:`canhost.bms.simulator.BmsSimulator` so CAN frame layouts remain
owned by the normal BMS protocol implementation instead of being duplicated
here.
"""

from __future__ import annotations

import math
import random
import threading
import time
from typing import Any, Callable

from .. import trust
from ..bms.simulator import BmsSimulator
from ..decoders import CanFrame
from . import fsae_telemetry_pb2 as pb


DEFAULT_MQTT_HOST = "bitfsae.com"
DEFAULT_MQTT_PORT = 1883
DEFAULT_MQTT_TOPIC = "fsae/telemetry"
DEFAULT_MQTT_USERNAME = "vehicle_gateway"
DEFAULT_SERIAL_BAUDRATE = 115200
DEFAULT_PCAN_CHANNEL = "PCAN_USBBUS1"
DEFAULT_PCAN_BITRATE = 500000
BASE_FREQUENCY_HZ = 10.0
BMS_DIVIDER = 5


def _enum_value(name: str, default_value: int) -> int:
    return int(getattr(pb, name, default_value))


def available_serial_ports() -> list[dict[str, str]]:
    """Return serial choices without making pyserial a startup dependency."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    return [
        {"device": item.device, "description": item.description or item.device}
        for item in sorted(list_ports.comports(), key=lambda value: value.device)
    ]


def _as_int(value: Any, label: str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label}必须是 {minimum}..{maximum} 的整数") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{label}必须在 {minimum}..{maximum}")
    return parsed


def normalize_simulator_config(config: dict[str, Any] | None) -> dict[str, Any]:
    """Validate a JS configuration object and return normalized values."""
    if not isinstance(config, dict):
        raise ValueError("模拟器配置必须是对象")
    mqtt_enabled = bool(config.get("mqtt", False))
    serial_enabled = bool(config.get("serial", False))
    pcan_enabled = bool(config.get("pcan", False))
    if not (mqtt_enabled or serial_enabled or pcan_enabled):
        raise ValueError("至少选择 MQTT、串口或 PCAN 中的一种输出")

    normalized: dict[str, Any] = {
        "mqtt": mqtt_enabled,
        "serial": serial_enabled,
        "pcan": pcan_enabled,
        "mqtt_host": str(config.get("mqtt_host") or DEFAULT_MQTT_HOST).strip(),
        "mqtt_port": _as_int(config.get("mqtt_port") or DEFAULT_MQTT_PORT,
                             "MQTT 端口", 1, 65535),
        "mqtt_topic": str(config.get("mqtt_topic") or DEFAULT_MQTT_TOPIC).strip(),
        "mqtt_username": str(config.get("mqtt_username") or "").strip(),
        "mqtt_password": str(config.get("mqtt_password") or ""),
        "mqtt_tls": bool(config.get("mqtt_tls", False)),
        "serial_port": str(config.get("serial_port") or "").strip(),
        "serial_baudrate": _as_int(
            config.get("serial_baudrate") or DEFAULT_SERIAL_BAUDRATE,
            "串口波特率", 1200, 4_000_000),
        "serial_suffix_hex": str(config.get("serial_suffix_hex") or "").strip().replace(" ", ""),
        "pcan_channel": str(config.get("pcan_channel") or DEFAULT_PCAN_CHANNEL).strip(),
        "pcan_bitrate": _as_int(config.get("pcan_bitrate") or DEFAULT_PCAN_BITRATE,
                                "PCAN 位率", 10_000, 1_000_000),
    }
    if mqtt_enabled:
        if not normalized["mqtt_host"]:
            raise ValueError("MQTT Broker 地址不能为空")
        topic = normalized["mqtt_topic"]
        if not topic or "+" in topic or "#" in topic:
            raise ValueError("MQTT Topic 必须是明确主题，不能使用通配符")
        if bool(normalized["mqtt_username"]) != bool(normalized["mqtt_password"]):
            raise ValueError("MQTT 用户名和密码必须同时填写或同时留空")
    if serial_enabled and not normalized["serial_port"]:
        raise ValueError("串口输出已启用，请选择串口")
    if pcan_enabled and not normalized["pcan_channel"]:
        raise ValueError("PCAN 输出已启用，请选择通道")
    try:
        bytes.fromhex(normalized["serial_suffix_hex"])
    except ValueError as exc:
        raise ValueError("串口包尾必须是偶数位十六进制，例如 0A 或 0D0A") from exc
    return normalized


class TelemetryFrameGenerator:
    """Generate the merged protobuf frame used by the gateway test script."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 random_seed: int = 473) -> None:
        self._clock = clock
        self._started_at = clock()
        self._last_physics_time = self._started_at
        self._random = random.Random(random_seed)
        self.rpm = 0
        self.speed = 0
        self.apps = 0
        self.brake = 0
        self.hv_voltage = 380.0
        self.hv_current = 0.0
        self.ivt_energy_wh = 0.0
        self.last_speed = 0
        self.motor_temp = 40.0
        self.state = "IDLE"
        self.frame_count = 0
        self.module_snapshots: list[dict[str, Any]] = []
        self.accel_x_g = 0.0
        self.accel_y_g = 0.0
        self.accel_z_g = 1.0
        self.yaw_rate_dps = 0.0
        self.yaw_deg = 0.0

    def _refresh_bms_cache(self) -> None:
        self.module_snapshots = []
        for module_index in range(6):
            base_voltage = 4000 + module_index * 10
            self.module_snapshots.append({
                "module_id": module_index + 1,
                "voltages": [base_voltage + self._random.randint(-15, 15) for _ in range(23)],
                "temps": [350 + module_index * 5 + self._random.randint(-5, 5) for _ in range(8)],
            })

    def _update_physics(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._last_physics_time)
        self._last_physics_time = now
        if self._random.random() < 0.05:
            self.state = self._random.choice(["ACCEL", "BRAKE", "COAST", "ACCEL"])
        if self.state == "ACCEL":
            self.apps = min(self.apps + 5, 100)
            self.brake = max(self.brake - 10, 0)
            self.rpm = min(self.rpm + 200 + self._random.randint(-50, 50), 12000)
            self.hv_current = self.apps * 2.0
        elif self.state == "BRAKE":
            self.apps = max(self.apps - 10, 0)
            self.brake = min(self.brake + 10, 80)
            self.rpm = max(self.rpm - 400, 0)
            self.hv_current = -20.0
        elif self.state == "COAST":
            self.apps = max(self.apps - 5, 0)
            self.brake = 0
            self.rpm = max(self.rpm - 100, 0)
            self.hv_current = 5.0
        self.motor_temp = (self.motor_temp + 0.05 if self.rpm > 5000
                           else max(self.motor_temp - 0.02, 30.0))
        self.hv_voltage = 380.0 - self.hv_current * 0.05 + self._random.uniform(-0.1, 0.1)
        self.ivt_energy_wh += self.hv_voltage * self.hv_current * elapsed / 3600.0
        self.speed = min(120, max(0, int(self.rpm / 90)))
        if elapsed > 0:
            self.accel_x_g = ((self.speed - self.last_speed) / 3.6) / elapsed / 9.80665
        self.accel_x_g = max(-1.5, min(1.5, self.accel_x_g))
        phase = self.frame_count / 18.0
        self.accel_y_g = 0.12 * math.sin(phase) if self.speed > 5 else 0.0
        self.accel_z_g = 1.0 + self._random.uniform(-0.015, 0.015)
        self.yaw_rate_dps = 8.0 * math.sin(phase) if self.speed > 5 else 0.0
        self.yaw_deg += self.yaw_rate_dps * elapsed
        if self.yaw_deg > 180.0:
            self.yaw_deg -= 360.0
        elif self.yaw_deg < -180.0:
            self.yaw_deg += 360.0
        self.last_speed = self.speed

    def _alarm_specs(self, maximum_temp: int, minimum_voltage: int) -> list[tuple[int, int, str]]:
        alarms: list[tuple[int, int, str]] = []
        if minimum_voltage < 3900:
            alarms.append((1001, _enum_value("ALARM_SEVERITY_WARNING", 2), "min cell voltage low"))
        if maximum_temp > 500:
            alarms.append((1002, _enum_value("ALARM_SEVERITY_ERROR", 3), "battery over temperature"))
        if self.hv_current < -10:
            alarms.append((1003, _enum_value("ALARM_SEVERITY_INFO", 1), "regen current active"))
        return alarms or [(1000, _enum_value("ALARM_SEVERITY_INFO", 1), "system nominal")]

    def generate_frame(self) -> Any:
        self._update_physics()
        self.frame_count += 1
        include_bms = self.frame_count % BMS_DIVIDER == 0
        if include_bms or not self.module_snapshots:
            self._refresh_bms_cache()

        voltages = [value for module in self.module_snapshots for value in module["voltages"]]
        temperatures = [value for module in self.module_snapshots for value in module["temps"]]
        maximum_voltage, minimum_voltage = max(voltages), min(voltages)
        maximum_temp, minimum_temp = max(temperatures), min(temperatures)
        maximum_voltage_index = voltages.index(maximum_voltage) + 1
        minimum_voltage_index = voltages.index(minimum_voltage) + 1
        maximum_temp_index = temperatures.index(maximum_temp) + 1
        minimum_temp_index = temperatures.index(minimum_temp) + 1
        soc_pct = max(0, min(100, int((self.hv_voltage - 320.0) / 0.7)))
        timestamp_ms = int((self._clock() - self._started_at) * 1000)

        frame = pb.TelemetryFrame()
        frame.timestamp_ms = timestamp_ms
        frame.frame_id = self.frame_count
        frame.hv_voltage = self.hv_voltage
        frame.hv_current = self.hv_current
        frame.battery_temp_max = maximum_temp / 10.0
        frame.ready_to_drive = int(self.rpm > 0)
        frame.vcu_status = 3 if self.rpm > 0 else 1
        frame.header.timestamp_ms = timestamp_ms
        frame.header.seq = self.frame_count
        frame.header.source_id = 1

        frame.fast_telemetry.hv_voltage_dv = round(self.hv_voltage * 10.0)
        frame.fast_telemetry.hv_current_ma = round(self.hv_current * 1000.0)
        frame.fast_telemetry.battery_temp_max_dc = maximum_temp
        frame.fast_telemetry.driving_mode = _enum_value("DRIVING_MODE_STRAIGHT", 2)
        frame.fast_telemetry.speed_kmh = self.speed
        frame.vehicle_state.speed_kmh = self.speed
        frame.vehicle_state.driving_mode = _enum_value("DRIVING_MODE_STRAIGHT", 2)
        frame.vehicle_state.throttle_position = self.apps
        frame.vehicle_state.brake_position = self.brake
        frame.vehicle_state.vcu_status = (_enum_value("VCU_STATUS_HV_ENABLED", 3)
                                          if self.rpm > 0 else _enum_value("VCU_STATUS_OFF", 1))
        frame.motion.gps_speed_kmh = self.speed
        frame.motion.accel_x_g = self.accel_x_g
        frame.motion.accel_y_g = self.accel_y_g
        frame.motion.accel_z_g = self.accel_z_g
        frame.motion.yaw_rate_dps = self.yaw_rate_dps
        frame.motion.yaw_deg = self.yaw_deg

        frame.ivt_telemetry.current_ma = round(self.hv_current * 1000.0)
        frame.ivt_telemetry.voltage_u1_mv = round(self.hv_voltage * 1000.0)
        frame.ivt_telemetry.voltage_u2_mv = round((self.hv_voltage - 1.5) * 1000.0)
        frame.ivt_telemetry.energy_wh = round(self.ivt_energy_wh)
        frame.ivt_telemetry.current_state = 0
        frame.ivt_telemetry.voltage_u1_state = 0
        frame.ivt_telemetry.voltage_u2_state = 0
        frame.ivt_telemetry.energy_state = 0
        frame.ivt_telemetry.power_w = round(self.hv_voltage * self.hv_current)
        frame.ivt_telemetry.power_state = 0
        frame.energy_meter.source = 1
        frame.energy_meter.current_ma = round(self.hv_current * 1000.0 * 1.002)
        frame.energy_meter.voltage_mv = round((self.hv_voltage - 1.4) * 1000.0)
        frame.energy_meter.power_w = round((self.hv_voltage - 1.4) * self.hv_current * 1.002)
        frame.energy_meter.energy_wh = round(self.ivt_energy_wh * 1.002)
        frame.energy_meter.current_state = 0
        frame.energy_meter.voltage_state = 0
        frame.energy_meter.power_state = 0
        frame.energy_meter.energy_state = 0

        frame.bms_telemetry.battery_state = 5
        frame.bms_telemetry.battery_alarm_level = 0
        frame.bms_telemetry.pos_relay_state = 1
        frame.bms_telemetry.neg_relay_state = 1
        frame.bms_telemetry.pre_relay_state = 0
        frame.bms_telemetry.charge_state = 0
        frame.bms_telemetry.charge_comm_state = 0
        frame.bms_telemetry.last_precharge_result = 1
        frame.bms_telemetry.last_precharge_success_ms = 2300
        frame.bms_telemetry.last_precharge_failure_ms = 0
        frame.imd_telemetry.resistance_kohm = 500
        frame.imd_telemetry.duty_pct_x10 = 500
        frame.imd_telemetry.frequency_hz_x100 = 8000
        frame.imd_telemetry.frequency_class = 0
        frame.imd_telemetry.status_code = 0
        frame.imd_telemetry.flags = 0x03
        frame.sop_limits.discharge_current_limit_a_x10 = 1000
        frame.sop_limits.charge_current_limit_a_x10 = 800
        frame.sop_limits.discharge_power_limit_kw_x10 = 4000
        frame.sop_limits.charge_power_limit_kw_x10 = 3000
        frame.sop_limits.sequence = 3
        frame.sop_limits.protocol_version = 1
        frame.sop_limits.bms_flags = 0x03
        frame.sop_limits.ecu_flags = 0x01
        frame.charger_telemetry.voltage_v = self.hv_voltage + 1.0
        frame.charger_telemetry.current_a = self.hv_current
        frame.charger_telemetry.protection = 0
        frame.charger_telemetry.output_state = 1

        frame.pdm_telemetry.bus_voltage_v = 13.8
        frame.pdm_telemetry.bus_current_a = 8.2
        frame.pdm_telemetry.bus_power_w = 113.0
        frame.pdm_telemetry.bus_energy_wh = 42.0
        frame.pdm_telemetry.battery_voltage_v = 13.2
        frame.pdm_telemetry.battery_current_a = -2.0
        frame.pdm_telemetry.battery_power_w = -26.0
        frame.pdm_telemetry.battery_energy_wh = 18.5
        fan = frame.fan_telemetry
        fan.fan1_rpm, fan.fan2_rpm, fan.fan3_rpm = 5200, 5100, 5300
        fan.pwm1_duty_pct, fan.pwm2_duty_pct = 82, 80
        fan.pwm1_target_pct, fan.pwm2_target_pct = 85, 83
        fan.ack_actual_pwm1_pct, fan.ack_actual_pwm2_pct = 82, 80
        fan.faults, fan.status_flags = 0, 1
        fan.max_motor_temp_dc, fan.max_controller_temp_dc = 720, 680
        fan.ack_result, fan.ack_mode, fan.ack_failsafe = 0, 1, 0
        fan.curve_temp_off, fan.curve_temp_on, fan.curve_temp_full = 30, 40, 55
        fan.curve_min_duty_pct, fan.curve_ramp_up_pct_per_s = 20, 10
        fan.failsafe_strategy = 1
        fan.failsafe_fallback1_pct, fan.failsafe_fallback2_pct = 40, 50
        fan.failsafe_stale_hold_s, fan.failsafe_ramp_down_pct_per_s = 10, 5
        fan.control_mode, fan.control_lease_remaining_s = 2, 30

        motor_specs = [
            ("MOTOR_POSITION_FRONT_LEFT", 1, 0.98, 1.02),
            ("MOTOR_POSITION_FRONT_RIGHT", 2, 1.00, 1.00),
            ("MOTOR_POSITION_REAR_LEFT", 3, 1.01, 0.99),
            ("MOTOR_POSITION_REAR_RIGHT", 4, 1.03, 0.97),
        ]
        for enum_name, default_position, rpm_scale, temp_scale in motor_specs:
            motor = frame.vehicle_state.motors.add()
            motor.position = _enum_value(enum_name, default_position)
            motor.rpm = int(self.rpm * rpm_scale)
            motor.torque_nm = int(self.hv_current * 0.8 * rpm_scale)
            motor.power_w = int(self.hv_voltage * self.hv_current * rpm_scale)
            motor.motor_temp_dc = round(self.motor_temp * temp_scale * 10.0)
            motor.inverter_temp_dc = round((self.motor_temp - 5.0) * temp_scale * 10.0)
            motor.motor_error = 0

        sensor_specs = [
            ("MOTOR_POSITION_FRONT_LEFT", 1, 6200),
            ("MOTOR_POSITION_FRONT_RIGHT", 2, 6400),
            ("MOTOR_POSITION_REAR_LEFT", 3, 6800),
            ("MOTOR_POSITION_REAR_RIGHT", 4, 7000),
        ]
        for enum_name, default_position, base_temp in sensor_specs:
            sensor = frame.thermal_summary.sensors.add()
            sensor.position = _enum_value(enum_name, default_position)
            sensor.min_temp_centi_c = base_temp - 180
            sensor.max_temp_centi_c = base_temp + 220
            sensor.avg_temp_centi_c = base_temp
            for chunk_index in range(4):
                chunk = sensor.chunks.add()
                chunk.position = sensor.position
                chunk.frame_id = self.frame_count
                chunk.chunk_index = chunk_index
                chunk.chunk_count = 4
                chunk.pixel_temp_centi_c = sensor.min_temp_centi_c + chunk_index * 120

        for alarm_id, severity, message in self._alarm_specs(maximum_temp, minimum_voltage):
            alarm = frame.alarms.add()
            alarm.alarm_id = alarm_id
            alarm.severity = severity
            alarm.message = message
        frame.battery_soc = soc_pct
        frame.max_cell_voltage = maximum_voltage
        frame.min_cell_voltage = minimum_voltage
        frame.max_cell_voltage_no = maximum_voltage_index
        frame.min_cell_voltage_no = minimum_voltage_index
        frame.max_temp = maximum_temp
        frame.min_temp = minimum_temp
        frame.max_temp_no = maximum_temp_index
        frame.min_temp_no = minimum_temp_index
        frame.battery_fault_code = 0
        for alarm_id, _severity, _message in self._alarm_specs(maximum_temp, minimum_voltage):
            if alarm_id == 1001:
                frame.battery_fault_code |= 0x01
            elif alarm_id == 1002:
                frame.battery_fault_code |= 0x02
            elif alarm_id == 1003:
                frame.battery_fault_code |= 0x04

        if include_bms:
            for module_data in self.module_snapshots:
                module = frame.modules.add()
                module.module_id = module_data["module_id"]
                for index, value in enumerate(module_data["voltages"], start=1):
                    setattr(module, f"v{index:02d}", value)
                for index, value in enumerate(module_data["temps"], start=1):
                    setattr(module, f"t{index}", value)
        return frame


def build_serial_packet(frame: Any, suffix_hex: str = "") -> bytes:
    return frame.SerializeToString() + bytes.fromhex(suffix_hex)


class TelemetrySimulatorService:
    """Own one local-simulator run and its MQTT/serial/PCAN resources."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._generation = 0
        self._state = "stopped"
        self._error: str | None = None
        self._config: dict[str, Any] = {}
        self._started_at: float | None = None
        self._stopped_at: float | None = None
        self._protobuf_frames = 0
        self._mqtt_frames = 0
        self._serial_frames = 0
        self._serial_bytes = 0
        self._pcan_frames = 0
        self._latest: dict[str, Any] | None = None

    def start(self, config: dict[str, Any] | None) -> dict[str, Any]:
        try:
            normalized = normalize_simulator_config(config)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return {"ok": False, "error": "遥测模拟器已在运行，请先停止再修改配置"}
            self._generation += 1
            generation = self._generation
            self._stop_event = threading.Event()
            self._state = "starting"
            self._error = None
            self._config = normalized
            self._started_at = self._clock()
            self._stopped_at = None
            self._protobuf_frames = 0
            self._mqtt_frames = 0
            self._serial_frames = 0
            self._serial_bytes = 0
            self._pcan_frames = 0
            self._latest = None
            self._thread = threading.Thread(
                target=self._run, args=(generation, normalized, self._stop_event),
                name="telemetry-local-simulator", daemon=True)
            self._thread.start()
        return {"ok": True, "state": "starting", "message": "本地遥测模拟器正在启动"}

    def stop(self) -> dict[str, Any]:
        with self._lock:
            thread = self._thread
            if thread is None or not thread.is_alive():
                self._state = "stopped"
                self._thread = None
                return {"ok": True, "unchanged": True}
            self._state = "stopping"
            self._stop_event.set()
        thread.join(timeout=3.0)
        with self._lock:
            still_running = thread.is_alive()
            if not still_running:
                self._thread = None
                self._state = "stopped"
            return ({"ok": False, "error": "模拟器正在等待系统连接调用返回，请稍后再次停止"}
                    if still_running else {"ok": True})

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            public_config = {key: value for key, value in self._config.items()
                             if key != "mqtt_password"}
            age = (None if self._started_at is None else max(
                0.0,
                (self._stopped_at if self._stopped_at is not None else self._clock())
                - self._started_at,
            ))
            return {
                "state": self._state,
                "running": self._state in {"starting", "running", "stopping"},
                "error": self._error,
                "config": public_config,
                "run_age": age,
                "protobuf_frames": self._protobuf_frames,
                "mqtt_frames": self._mqtt_frames,
                "serial_frames": self._serial_frames,
                "serial_bytes": self._serial_bytes,
                "pcan_frames": self._pcan_frames,
                "latest": dict(self._latest) if self._latest is not None else None,
                "base_frequency_hz": BASE_FREQUENCY_HZ,
                "bms_frequency_hz": BASE_FREQUENCY_HZ / BMS_DIVIDER,
            }

    def _set_error(self, generation: int, message: str) -> None:
        with self._lock:
            if generation == self._generation:
                self._error = message
                self._state = "error"

    def _run(self, generation: int, config: dict[str, Any],
             stop_event: threading.Event) -> None:
        mqtt_client: Any = None
        serial_port: Any = None
        pcan_bus: Any = None
        bms_simulator: BmsSimulator | None = None
        try:
            if config["mqtt"]:
                import paho.mqtt.client as mqtt
                mqtt_connect_done = threading.Event()
                mqtt_connect_error: list[str] = []
                mqtt_client = mqtt.Client(
                    mqtt.CallbackAPIVersion.VERSION2,
                    client_id=f"can-host-sim-{int(time.time() * 1000):x}",
                    clean_session=True,
                    protocol=mqtt.MQTTv311,
                )
                def on_connect(client: Any, userdata: Any, flags: Any,
                               reason_code: Any, properties: Any = None) -> None:
                    value = getattr(reason_code, "value", reason_code)
                    try:
                        code = int(value)
                    except (TypeError, ValueError):
                        code = -1
                    if code != 0:
                        mqtt_connect_error.append(f"MQTT 连接被拒绝（{reason_code}）")
                    mqtt_connect_done.set()

                def on_connect_fail(client: Any, userdata: Any) -> None:
                    mqtt_connect_error.append("无法连接 MQTT Broker，请检查网络、地址和端口")
                    mqtt_connect_done.set()

                def on_disconnect(client: Any, userdata: Any, disconnect_flags: Any,
                                  reason_code: Any, properties: Any = None) -> None:
                    if stop_event.is_set():
                        return
                    value = getattr(reason_code, "value", reason_code)
                    try:
                        code = int(value)
                    except (TypeError, ValueError):
                        code = -1
                    self._set_error(
                        generation,
                        "MQTT 连接意外断开" if code == 0
                        else f"MQTT 连接异常断开（{reason_code}）",
                    )
                    stop_event.set()

                mqtt_client.on_connect = on_connect
                mqtt_client.on_connect_fail = on_connect_fail
                mqtt_client.on_disconnect = on_disconnect
                if config["mqtt_username"]:
                    mqtt_client.username_pw_set(config["mqtt_username"], config["mqtt_password"])
                if config["mqtt_tls"]:
                    mqtt_client.tls_set_context(trust.https_ssl_context())
                mqtt_client.connect(config["mqtt_host"], config["mqtt_port"], 30)
                mqtt_client.loop_start()
                deadline = time.monotonic() + 10.0
                while not mqtt_connect_done.is_set() and not stop_event.is_set():
                    mqtt_connect_done.wait(min(0.1, max(0.0, deadline - time.monotonic())))
                    if time.monotonic() >= deadline:
                        raise RuntimeError("MQTT 连接确认超时，请检查网络、地址和端口")
                if stop_event.is_set():
                    return
                if mqtt_connect_error:
                    raise RuntimeError(mqtt_connect_error[-1])
            if config["serial"]:
                try:
                    import serial
                except ImportError as exc:
                    raise RuntimeError("缺少 pyserial，请重新安装上位机依赖") from exc
                serial_port = serial.Serial(
                    port=config["serial_port"], baudrate=config["serial_baudrate"],
                    bytesize=8, parity="N", stopbits=1, timeout=1.0,
                )
            if config["pcan"]:
                import can
                pcan_bus = can.Bus(
                    interface="pcan", channel=config["pcan_channel"],
                    bitrate=config["pcan_bitrate"], receive_own_messages=False,
                )

                def send_can(frame: CanFrame) -> None:
                    try:
                        pcan_bus.send(can.Message(
                            arbitration_id=frame.arbitration_id,
                            is_extended_id=frame.is_extended_id,
                            data=frame.data,
                        ))
                    except Exception as exc:
                        self._set_error(generation, f"PCAN 发送失败：{exc}")
                        stop_event.set()
                        return
                    with self._lock:
                        if generation == self._generation:
                            self._pcan_frames += 1

                bms_simulator = BmsSimulator(send_can, "can1")
                bms_simulator.start()

            generator = TelemetryFrameGenerator(clock=self._clock)
            with self._lock:
                if generation == self._generation:
                    self._state = "running"
            interval = 1.0 / BASE_FREQUENCY_HZ
            while not stop_event.is_set():
                cycle_started = self._clock()
                frame = generator.generate_frame()
                payload = frame.SerializeToString()
                if mqtt_client is not None:
                    result = mqtt_client.publish(config["mqtt_topic"], payload, qos=0)
                    if int(result.rc) != 0:
                        raise RuntimeError(f"MQTT 发布失败（代码 {result.rc}）")
                if serial_port is not None:
                    packet = payload + bytes.fromhex(config["serial_suffix_hex"])
                    written = serial_port.write(packet)
                    serial_port.flush()
                    if written != len(packet):
                        raise RuntimeError(f"串口只写入 {written}/{len(packet)} 字节")
                with self._lock:
                    if generation != self._generation:
                        break
                    self._protobuf_frames += 1
                    self._mqtt_frames += int(mqtt_client is not None)
                    self._serial_frames += int(serial_port is not None)
                    self._serial_bytes += len(packet) if serial_port is not None else 0
                    self._latest = {
                        "sequence": int(frame.header.seq),
                        "timestamp_ms": int(frame.header.timestamp_ms),
                        "state": generator.state,
                        "rpm": int(frame.vehicle_state.motors[0].rpm),
                        "speed_kmh": int(frame.vehicle_state.speed_kmh),
                        "hv_voltage_v": round(float(frame.hv_voltage), 1),
                        "hv_current_a": round(float(frame.hv_current), 1),
                        "soc_pct": int(frame.battery_soc),
                        "module_count": len(frame.modules),
                        "payload_bytes": len(payload),
                    }
                remaining = interval - (self._clock() - cycle_started)
                if remaining > 0:
                    stop_event.wait(remaining)
        except Exception as exc:
            self._set_error(generation, f"本地遥测模拟器停止：{exc}")
        finally:
            if bms_simulator is not None:
                bms_simulator.stop()
            if mqtt_client is not None:
                try:
                    mqtt_client.disconnect()
                except Exception:
                    pass
                try:
                    mqtt_client.loop_stop()
                except Exception:
                    pass
            if serial_port is not None:
                try:
                    serial_port.close()
                except Exception:
                    pass
            if pcan_bus is not None:
                try:
                    pcan_bus.shutdown()
                except Exception:
                    pass
            with self._lock:
                if generation == self._generation:
                    if self._state not in {"error", "stopped"}:
                        self._state = "stopped"
                    self._stopped_at = self._clock()
                    self._thread = None


__all__ = [
    "BASE_FREQUENCY_HZ", "BMS_DIVIDER", "TelemetryFrameGenerator",
    "TelemetrySimulatorService", "available_serial_ports", "build_serial_packet",
    "normalize_simulator_config",
]
