Brake system requirements
=========================

.. sysreq:: Braking distance
   :id: SYS_001
   :status: approved
   :tags: brake
   :safety: C
   :rev: 3
   :reviewer: R. Team

   The vehicle **shall** stop within 40 m from 100 km/h.
   :np:`(p1) The brake request reaches the actuator within 50 ms.`

.. sysreq:: Parking hold
   :id: SYS_002
   :status: obsolete
   :rev: 1

   The vehicle shall hold on a 20 % slope.

.. swreq:: Brake request latency
   :id: SW_001
   :status: approved
   :safety: B(C)
   :rev: 2
   :parent: SYS_001.p1

   The software shall forward a brake request within 10 ms, as :need:`SYS_001` requires.

.. swreq:: Brake request plausibility
   :id: SW_002
   :status: open
   :parent: SYS_001[status=="approved"], SW_001

   The software shall reject a brake request outside 0 to 100 %.

.. swreq:: Brake light
   :id: SW_003
   :status: open
   :safety: QM
   :parent: SYS_001.p1[[tags[0]=="brake"]], SYS_001

   The software shall switch the brake light on with every brake request.

.. test:: Brake request latency test
   :id: TC_001
   :status: passed
   :verifies: SW_001, SW_002

   Measure the latency from the request to the actuator.
