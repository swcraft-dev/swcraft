Brake system requirements
=========================

.. sysreq:: Braking distance
   :id: SYS_001
   :status: approved
   :safety: C

   The vehicle shall stop within 40 m from 100 km/h.

.. sysreq:: Brake request latency
   :id: SYS_002
   :status: approved
   :safety: C
   :parent: SYS_001

   A brake request shall reach the actuator within 50 ms.

.. sysreq:: Brake light
   :id: SYS_003
   :status: approved
   :safety: QM
   :parent: SYS_001

   The brake light shall be on with every brake request.

Requirements from the requirements tool
---------------------------------------

The team's requirements tool exports these as a ``needs.json`` of their own; their ids hold ``[``.

.. needimport:: doors.json

SWCraft's architecture
----------------------

.. needtable::
   :filter: is_external
   :columns: id, title, type, tags, satisfies, satisfies_unconfirmed, derived_from, derived_from_unconfirmed
   :style: table
