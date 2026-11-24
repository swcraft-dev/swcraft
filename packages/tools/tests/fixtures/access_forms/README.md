# `access_forms/` — the implicit access and asynchronous call fixture (contract `12.7.0`)

`model/` is a **committed model tree used through a copy**, and it is deliberately **not** a
golden: no `.arxml`, diagram or needs file of it is committed anywhere. It is a small brake system
whose runnables use the forms contract `12.7.0` adds: an implicit read and an implicit write
(`by: implicit`), an asynchronous call the caller polls (`asynchronous: true`, `poll: true`, a
stated `timeout`), an asynchronous call whose return starts another runnable (a `call_returned`
event), beside the forms before it (an explicit read by argument and by value, an explicit write, a
synchronous call). It is the shared fixture of the access-form tests and the basis of the walk kit.

| Element | What it is here for |
|---|---|
| `swc/chassis/WheelSensor.yaml` | the sender of the wheel speed; `SampleSpeed` **writes it implicitly**. Placed twice, so one type has two occurrences |
| `swc/chassis/BrakePedal.yaml` | the sender of the pedal position; `SamplePedal` writes it **explicitly** |
| `swc/chassis/BrakeController.yaml` | the receiver and the client. `ComputeBrakeTorque` reads the wheel speed **implicitly** and the pedal **by argument**, writes the torque **implicitly**, calls `rNvStorage.WriteBlock` **asynchronously** and polls `rCalibration.ReadBlock` (`poll: true`, `timeout: 0.5`); `OnBlockWritten` is started by the **`call_returned`** event of `WriteBlock`; `Diagnose` reads the wheel speed implicitly, the pedal **by value**, and calls `rCalibration.GetVersion` **synchronously** |
| `swc/chassis/BrakeActuator.yaml` | the receiver of the torque; `ApplyTorque` reads it **implicitly** |
| `swc/chassis/NvStorage.yaml` | the server of the three operations, one operation-event runnable each |
| `interfaces/` | three sender/receiver interfaces of one element each, two client/server interfaces |
| `compositions/Root.yaml` | the root, placing every component once (`FrontWheelSensor` for the sensor) and `RearAxle` |
| `compositions/RearAxle.yaml` | the sensor's second occurrence, `RearAxle/Sensor` |
| `connectors/` | the five links of the root |
| `tasks/` | `Task_10ms` maps `FrontWheelSensor`'s `SampleSpeed` by its instance path, `SamplePedal` and `ComputeBrakeTorque`; `Task_100ms` maps `ApplyTorque`, `OnBlockWritten` and the server runnables. `Diagnose` and the rear sensor's `SampleSpeed` are in no task |
| `hardware/`, `data_types/` | the processor, one core and its partition; the one implementation type and its base |

`swcraft resource-report` over it ends with the `IMPLICIT ACCESS` section:

```text
IMPLICIT ACCESS (implicit reads and writes the task's runnables declare; when the RTE copies them, and what that costs, is not in the model)
  Task        Core   Implicit reads  Implicit writes  Runnables
  Task_10ms   CORE0  1               2                2
  Task_100ms  CORE0  1               0                1
  RUNNABLES IN NO TASK
    Runnable                     Implicit reads  Implicit writes
    BrakeController.Diagnose     1               0
    RearAxle/Sensor.SampleSpeed  0               1
```

## Why it is not in the byte-identity record

The three record tests (`test_external_stubs_byte_identity.py`, `test_diagram_switch_byte_identity.py`
and `test_application_types_byte_identity.py`) recognise a committed model tree by a YAML file in its
`compositions/` layer, and this tree has one. It is discarded there **by name**, as
`data_description/model` is: no branch point before contract `12.7.0` could load it, so the record's
claim — the bytes the generators wrote before — cannot cover it, and a row would move every sibling
gate's `RECORD_DIGEST`.

## Changing this tree

The access-form tests read it: the validator's rules, the writer at both versions, the importer
round trip and the component exchange; so do the byte-identity records, the termination re-freeze,
the XSD conformance suite and `packages/mcp/tests` (`grep -rln access_forms packages` lists them,
and this list is not checked). A changed element moves their expectations, and the section
above moves `packages/tools/tests/test_implicit_access_report.py` and
`packages/mcp/tests/test_resource_report_tool.py`.
