-- =============================================================================
-- escalation_db — 3NF seed data (transactional tables only; lookups live in schema.sql)
-- Run after schema: python db/setup.py
--
-- Web logins (password for all): password123
--   planner_user    → Planner
--   dispatcher_user → Dispatcher
--   manager_user    → Fleet Manager
--
-- Driver app: last 4 digits of phone (Mubarak Ali → 4567)
-- Staggered 12-hour shifts (24/7 coverage, two workers per calendar day):
--   Mubarak & Siti — Mon/Wed/Fri/Sun (06:00–18:00 then 18:00–06:00)
--   Marcus & Rohan — Tue/Thu/Sat
-- =============================================================================

USE escalation_db;

SET FOREIGN_KEY_CHECKS = 0;
TRUNCATE TABLE alerts_log;
TRUNCATE TABLE port_slot_bookings;
TRUNCATE TABLE delivery_completions;
TRUNCATE TABLE job_rejections;
TRUNCATE TABLE dispatch_assignments;
TRUNCATE TABLE truck_allocations;
TRUNCATE TABLE driver_schedules;
TRUNCATE TABLE events;
TRUNCATE TABLE containers;
TRUNCATE TABLE vessel_tracking;
TRUNCATE TABLE voyages;
TRUNCATE TABLE vessels;
TRUNCATE TABLE driver_locations;
TRUNCATE TABLE drivers;
TRUNCATE TABLE users;
SET FOREIGN_KEY_CHECKS = 1;

INSERT INTO users (username, email, password_hash, role_id) VALUES
('planner_user', 'planner@logistics.com', 'scrypt:32768:8:1$3oODJ0IJIeh5RrSO$80664d851b7e1ec84e08f9e3189558bcb9c8e89eb97cd97d9d9109c4dd7d5383f6e6a12f7a4928526cdcd8c2de436803e744a8b4b7b120e524a18482f18a3931', 1),
('dispatcher_user', 'dispatcher@logistics.com', 'scrypt:32768:8:1$3oODJ0IJIeh5RrSO$80664d851b7e1ec84e08f9e3189558bcb9c8e89eb97cd97d9d9109c4dd7d5383f6e6a12f7a4928526cdcd8c2de436803e744a8b4b7b120e524a18482f18a3931', 2),
('manager_user', 'manager@logistics.com', 'scrypt:32768:8:1$3oODJ0IJIeh5RrSO$80664d851b7e1ec84e08f9e3189558bcb9c8e89eb97cd97d9d9109c4dd7d5383f6e6a12f7a4928526cdcd8c2de436803e744a8b4b7b120e524a18482f18a3931', 3);

INSERT INTO drivers (driver_name, phone_number, status_code) VALUES
('Mubarak Ali', '+65 9123 4567', 'Available'),
('Siti Aisha', '+65 9876 5432', 'Available'),
('Marcus Chen', '+65 8123 9876', 'Available'),
('Rohan Raj', '+65 9001 2345', 'Available'),
('Emergency Contractor', '+65 9999 0000', 'Available');

-- Marcus (9876): ~2 km from Tuas port — demo: port pickup (slot freed) → warehouse delivery
INSERT INTO driver_locations (driver_id, latitude, longitude, heading, speed_kph, recorded_at) VALUES
(1, 1.3340, 103.7070, 45.0, 0.0, NOW()),
(2, 1.3335, 103.7065, 120.0, 0.0, NOW()),
(3, 1.2990, 103.6310, 180.0, 45.0, NOW()),
(4, 1.3338, 103.7075, 310.0, 0.0, NOW());

INSERT INTO driver_schedules (driver_id, day_of_week, shift_start, shift_end) VALUES
(1, 0, '06:00:00', '18:00:00'), (1, 2, '06:00:00', '18:00:00'), (1, 4, '06:00:00', '18:00:00'), (1, 6, '06:00:00', '18:00:00'),
(2, 0, '18:00:00', '06:00:00'), (2, 2, '18:00:00', '06:00:00'), (2, 4, '18:00:00', '06:00:00'), (2, 6, '18:00:00', '06:00:00'),
(3, 1, '06:00:00', '18:00:00'), (3, 3, '06:00:00', '18:00:00'), (3, 5, '06:00:00', '18:00:00'),
(4, 1, '18:00:00', '06:00:00'), (4, 3, '18:00:00', '06:00:00'), (4, 5, '18:00:00', '06:00:00');

INSERT INTO vessels (vessel_id, vessel_name) VALUES
('VES-COSCO-88', 'Cosco Shipping Alps'),
('VES-MAERSK-12', 'Maersk Mc-Kinney Moller'),
('VES-ONE-CYGNUS', 'ONE Cygnus'),
('VES-EVER-GIVEN', 'Ever Given'),
('VES-HMM-ALG', 'HMM Algeciras'),
('VES-MSC-GUL', 'MSC Gulsun'),
('VES-CMA-MPO', 'CMA CGM Marco Polo'),
('VES-YML-UTM', 'Yang Ming Utmost'),
('VES-HLC-BER', 'Hapag-Lloyd Berlin Express'),
('VES-PIL-KOT', 'PIL Kota Cabot'),
('VES-ZIM-SGP', 'ZIM Singapore'),
('VES-WHL-282', 'Wan Hai 282');

INSERT INTO voyages (voyage_id, vessel_id, voyage_number) VALUES
('V-COSCO-88', 'VES-COSCO-88', '045W'),
('V-MAERSK-12', 'VES-MAERSK-12', '261E'),
('V-ONE-CYGNUS', 'VES-ONE-CYGNUS', '014N'),
('V-EVER-GIVEN', 'VES-EVER-GIVEN', '0993-02B'),
('V-HMM-ALG', 'VES-HMM-ALG', '012E'),
('V-MSC-GUL', 'VES-MSC-GUL', '318W'),
('V-CMA-MPO', 'VES-CMA-MPO', '0FMLW1'),
('V-YML-UTM', 'VES-YML-UTM', '088E'),
('V-HLC-BER', 'VES-HLC-BER', '051N'),
('V-PIL-KOT', 'VES-PIL-KOT', '024S'),
('V-ZIM-SGP', 'VES-ZIM-SGP', '7E'),
('V-WHL-282', 'VES-WHL-282', 'E006');

INSERT INTO vessel_tracking (voyage_id, latitude, longitude, heading, speed_knots, eta_datetime, tracking_status_code) VALUES
('V-COSCO-88', 1.1200, 103.7100, 15.0, 11.0, DATE_ADD(NOW(), INTERVAL 3 HOUR), 'Approaching'),
('V-MAERSK-12', 1.3015, 103.6340, 0.0, 0.0, NOW(), 'At Berth'),
('V-ONE-CYGNUS', 1.0800, 103.6950, 20.0, 10.5, DATE_ADD(NOW(), INTERVAL 5 HOUR), 'At Sea'),
('V-EVER-GIVEN', 1.2000, 103.6600, 10.0, 9.0, DATE_ADD(NOW(), INTERVAL 90 MINUTE), 'Approaching'),
('V-HMM-ALG', 1.1500, 103.6750, 12.0, 10.0, DATE_ADD(NOW(), INTERVAL 2 HOUR), 'Approaching'),
('V-MSC-GUL', 1.3014, 103.6342, 0.0, 0.0, NOW(), 'At Berth'),
('V-CMA-MPO', 1.0950, 103.7000, 18.0, 11.5, DATE_ADD(NOW(), INTERVAL 4 HOUR), 'At Sea'),
('V-YML-UTM', 1.0700, 103.7150, 22.0, 9.5, DATE_ADD(NOW(), INTERVAL 6 HOUR), 'At Sea'),
('V-HLC-BER', 1.2400, 103.6500, 5.0, 8.0, DATE_ADD(NOW(), INTERVAL 45 MINUTE), 'Berthing'),
('V-PIL-KOT', 1.1300, 103.6900, 14.0, 10.0, DATE_ADD(NOW(), INTERVAL 150 MINUTE), 'Approaching'),
('V-ZIM-SGP', 1.3016, 103.6338, 0.0, 0.0, NOW(), 'At Berth'),
('V-WHL-282', 1.0600, 103.7250, 25.0, 12.0, DATE_ADD(NOW(), INTERVAL 8 HOUR), 'At Sea');

INSERT INTO containers (container_number, voyage_id, discharge_datetime, lfd_datetime, import_status) VALUES
('TEMU4810239', 'V-MAERSK-12', DATE_SUB(NOW(), INTERVAL 3 DAY), DATE_ADD(NOW(), INTERVAL 6 HOUR), 'Imported'),
('NYKU9012455', 'V-COSCO-88', DATE_SUB(NOW(), INTERVAL 2 DAY), DATE_ADD(NOW(), INTERVAL 18 HOUR), 'Imported'),
('EMCU5561230', 'V-EVER-GIVEN', DATE_SUB(NOW(), INTERVAL 1 DAY), DATE_ADD(NOW(), INTERVAL 48 HOUR), 'Imported'),
('REDU0000001', 'V-COSCO-88', DATE_SUB(NOW(), INTERVAL 1 DAY), DATE_ADD(NOW(), INTERVAL 4 HOUR), 'Imported'),
('MSKU7723401', 'V-MSC-GUL', DATE_SUB(NOW(), INTERVAL 2 DAY), DATE_ADD(NOW(), INTERVAL 8 HOUR), 'Imported'),
('CMAU3344556', 'V-ZIM-SGP', DATE_SUB(NOW(), INTERVAL 1 DAY), DATE_ADD(NOW(), INTERVAL 12 HOUR), 'Imported'),
('ONEU8899001', 'V-HLC-BER', DATE_SUB(NOW(), INTERVAL 12 HOUR), DATE_ADD(NOW(), INTERVAL 3 HOUR), 'Imported'),
('HLCU5566778', 'V-PIL-KOT', DATE_SUB(NOW(), INTERVAL 1 DAY), DATE_ADD(NOW(), INTERVAL 24 HOUR), 'Imported'),
('ZIMU1122334', 'V-CMA-MPO', DATE_SUB(NOW(), INTERVAL 6 HOUR), DATE_ADD(NOW(), INTERVAL 36 HOUR), 'Imported'),
('WHLU9988776', 'V-YML-UTM', DATE_SUB(NOW(), INTERVAL 8 HOUR), DATE_ADD(NOW(), INTERVAL 72 HOUR), 'Imported');

INSERT INTO events (container_number, source_api, event_type, raw_payload) VALUES
('TEMU4810239', 'PORTNET_AIS_SIMULATOR', 'SIMULATED_VESSEL_DISCHARGE', '{"voyage_id":"V-MAERSK-12"}'),
('NYKU9012455', 'PORTNET_AIS_SIMULATOR', 'SIMULATED_VESSEL_DISCHARGE', '{"voyage_id":"V-COSCO-88"}'),
('MSKU7723401', 'PORTNET_AIS_SIMULATOR', 'SIMULATED_VESSEL_DISCHARGE', '{"voyage_id":"V-MSC-GUL"}'),
('CMAU3344556', 'PORTNET_AIS_SIMULATOR', 'SIMULATED_VESSEL_DISCHARGE', '{"voyage_id":"V-ZIM-SGP"}'),
('ONEU8899001', 'PORTNET_AIS_SIMULATOR', 'SIMULATED_VESSEL_DISCHARGE', '{"voyage_id":"V-HLC-BER"}');
