"""AUTOSAR-compatible identifier utilities."""

import re
from collections.abc import Iterable

IDENTIFIER = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")


#: The AUTOSAR ``Identifier`` maximum (``IDENTIFIER--SIMPLE``'s ``maxLength``).
MAX_IDENTIFIER_LENGTH = 128
#: The limit of a WATERS preset's generated names; a specimen's per-layer limits replace it
#: through ``limit=``.
GENERATED_IDENTIFIER_LENGTH = 60


def validate_identifier(
    name: str, context: str = "identifier", *, generated: bool = False, limit: int | None = None
) -> str:
    """A legal identifier of at most ``limit`` characters.

    Without ``limit``, the limit is 60 when ``generated`` and 128 otherwise.
    """
    if limit is None:
        limit = GENERATED_IDENTIFIER_LENGTH if generated else MAX_IDENTIFIER_LENGTH
    if not isinstance(name, str) or not IDENTIFIER.fullmatch(name) or len(name) > limit:
        raise ValueError(f"invalid {context} {name!r}: expected [a-zA-Z][a-zA-Z0-9_]*, max {limit} characters")
    return name


def validate_unique_identifiers(names: Iterable[str], context: str = "identifiers") -> None:
    seen: dict[str, str] = {}
    for name in names:
        validate_identifier(name, context)
        key = name.casefold()
        if key in seen:
            raise ValueError(f"duplicate {context} ignoring case: {name!r} conflicts with {seen[key]!r}")
        seen[key] = name


def component_name(domain_index: int, component_index: int) -> str:
    return validate_identifier(f"Dom{domain_index:02d}_Swc{component_index:03d}", "component name", generated=True)


# The catalogue is data, not a PRNG choice, so changing presentation names never changes graph draws.
DOMAIN_CATALOG = (
    (
        "powertrain",
        "Powertrain",
        "Synthetic powertrain coordination domain.",
        (
            "DriverDemandInterpreter",
            "TorqueRequestCoordinator",
            "EngineStateEstimator",
            "ActuatorRequestPublisher",
            "PowertrainSignalProcessor",
        ),
    ),
    (
        "chassis",
        "Chassis",
        "Synthetic chassis state and command domain.",
        (
            "WheelSpeedAcquisition_FL",
            "WheelSpeedAcquisition_FR",
            "VehicleSpeedProvider",
            "BrakeCommandCoordinator",
            "ChassisSignalProcessor",
        ),
    ),
    (
        "vehicle_services",
        "VehicleServices",
        "Synthetic vehicle service coordination domain.",
        (
            "VehicleModeManager",
            "DiagnosticEventRouter",
            "VehicleStateService",
            "ServiceRequestBroker",
            "VehicleSignalProcessor",
        ),
    ),
    (
        "energy_management",
        "EnergyManagement",
        "Synthetic energy state and request domain.",
        (
            "BatteryStateEstimator",
            "EnergyFlowCoordinator",
            "ChargeRequestManager",
            "EnergySignalProcessor",
            "EnergyHealthMonitor",
        ),
    ),
    (
        "thermal_management",
        "ThermalManagement",
        "Synthetic thermal load and cooling domain.",
        (
            "ThermalLoadEstimator",
            "ThermalCoordinationManager",
            "CoolingRequestCoordinator",
            "ThermalHealthMonitor",
            "ThermalSignalProcessor",
        ),
    ),
    (
        "body_comfort",
        "BodyComfort",
        "Synthetic cabin and exterior comfort domain.",
        (
            "CabinClimateCoordinator",
            "ZoneTemperatureMonitor_01",
            "ZoneTemperatureMonitor_02",
            "ExteriorLightingController",
            "ComfortSignalProcessor",
        ),
    ),
    (
        "safety_supervision",
        "SafetySupervision",
        "Synthetic safety observation and supervision domain.",
        (
            "SafetyStateMonitor",
            "FaultContainmentCoordinator",
            "SafetyEventEvaluator",
            "SupervisionSignalProcessor",
            "SafetyHealthReporter",
        ),
    ),
    (
        "platform_services",
        "PlatformServices",
        "Synthetic platform timing and configuration domain.",
        (
            "TimeSynchronizationService",
            "ConfigurationDataProvider",
            "CentralCommunicationGateway",
            "PlatformHealthMonitor",
            "PlatformSignalProcessor",
        ),
    ),
)


RUNNABLE_NAMES = (
    "AcquireInputs",
    "UpdateState",
    "EvaluateConditions",
    "ComputeCommand",
    "PublishOutputs",
    "MonitorHealth",
    "HandleRequests",
)
SIGNAL_NAMES = (
    "VehicleSpeed",
    "TorqueRequest",
    "OperatingMode",
    "ThermalState",
    "DiagnosticEvent",
    "HealthStatus",
    "CommandValidity",
    "QualityState",
    "UpdateCounter",
)
SERVICE_NAMES = ("ReadStatus", "SubmitRequest", "UpdateConfiguration", "ExecuteCommand")
ARGUMENT_NAMES = ("Request", "Response", "Context", "Command", "Status")


def domain_catalog(domain_index: int) -> tuple[str, str, str, tuple[str, ...]]:
    if 1 <= domain_index <= len(DOMAIN_CATALOG):
        return DOMAIN_CATALOG[domain_index - 1]
    token = f"synthetic_domain_{domain_index:02d}"
    title = f"SyntheticDomain{domain_index:02d}"
    return (
        token,
        title,
        "Synthetic extension domain for scalable generation.",
        (f"{title}Coordinator",),
    )


def semantic_component_name(domain_index: int, position: int) -> str:
    token, title, _, roles = domain_catalog(domain_index)
    if position < len(roles):
        return validate_identifier(roles[position], "component name", generated=True)
    base = f"{title}SignalProcessor"
    ordinal = position - len(roles) + 1
    return validate_identifier(f"{base}_{ordinal:02d}", "component name", generated=True)


def semantic_runnable_name(index: int) -> str:
    base = RUNNABLE_NAMES[(index - 1) % len(RUNNABLE_NAMES)]
    cycle = (index - 1) // len(RUNNABLE_NAMES)
    return validate_identifier(base if cycle == 0 else f"{base}_{cycle + 1:02d}", "runnable name", generated=True)


def indexed_name(prefix: str, index: int, context: str = "generated name") -> str:
    return validate_identifier(f"{prefix}_{index:03d}", context, generated=True)
