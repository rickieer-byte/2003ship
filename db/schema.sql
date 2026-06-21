-- =============================================================================
-- escalation_db — 3NF-normalized schema (PythonAnywhere / local MySQL)
--
-- Normalization notes:
--   • Lookup tables: roles, driver_statuses, dispatch_statuses, tracking_statuses
--   • Telemetry isolated: driver_locations, vessel_tracking (time-varying facts)
--   • Dispatch lifecycle on truck_allocations only (no duplicate at_port on containers)
--   • Driver assignment history in dispatch_assignments (outcome via assignment_outcomes FK)
--   • job_rejections references assignment_id only (no redundant container/driver columns)
--   • delivery_completions holds POD facts; driver/container derived via assignment joins
--   • Referential integrity via FK constraints throughout
--
-- Run once: python db/setup.py   OR   mysql -u USER -p < db/schema.sql
-- =============================================================================

CREATE DATABASE IF NOT EXISTS escalation_db
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE escalation_db;

SET FOREIGN_KEY_CHECKS = 0;
DROP TABLE IF EXISTS leave_requests;
DROP TABLE IF EXISTS alerts_log;
DROP TABLE IF EXISTS port_slot_bookings;
DROP TABLE IF EXISTS delivery_completions;
DROP TABLE IF EXISTS job_rejections;
DROP TABLE IF EXISTS dispatch_assignments;
DROP TABLE IF EXISTS assignment_outcomes;
DROP TABLE IF EXISTS truck_allocations;
DROP TABLE IF EXISTS driver_schedules;
DROP TABLE IF EXISTS events;
DROP TABLE IF EXISTS containers;
DROP TABLE IF EXISTS vessel_tracking;
DROP TABLE IF EXISTS voyages;
DROP TABLE IF EXISTS vessels;
DROP TABLE IF EXISTS driver_locations;
DROP TABLE IF EXISTS drivers;
DROP TABLE IF EXISTS users;
DROP TABLE IF EXISTS roles;
DROP TABLE IF EXISTS driver_statuses;
DROP TABLE IF EXISTS dispatch_statuses;
DROP TABLE IF EXISTS tracking_statuses;
DROP TABLE IF EXISTS ports;
DROP TABLE IF EXISTS warehouses;
-- SET FOREIGN_KEY_CHECKS = 1;


-- ---------------------------------------------------------------------------
-- Reference / lookup entities (eliminate repeating domain values)
-- ---------------------------------------------------------------------------
CREATE TABLE roles (
    role_id TINYINT UNSIGNED PRIMARY KEY,
    role_name VARCHAR(20) NOT NULL UNIQUE
);

CREATE TABLE driver_statuses (
    status_code VARCHAR(20) PRIMARY KEY,
    description VARCHAR(100) NOT NULL
);

CREATE TABLE dispatch_statuses (
    status_code VARCHAR(20) PRIMARY KEY,
    description VARCHAR(100) NOT NULL
);

CREATE TABLE tracking_statuses (
    status_code VARCHAR(20) PRIMARY KEY,
    description VARCHAR(100) NOT NULL
);

CREATE TABLE ports (
    port_id VARCHAR(20) PRIMARY KEY,
    port_name VARCHAR(100) NOT NULL,
    latitude DECIMAL(10, 6) NOT NULL,
    longitude DECIMAL(10, 6) NOT NULL,
    geofence_radius_km DECIMAL(4, 2) NOT NULL DEFAULT 2.00,
    max_prime_movers TINYINT UNSIGNED NOT NULL DEFAULT 3
        COMMENT 'Concurrent prime-mover slots at gate (PSA mock limit)'
);

INSERT INTO roles (role_id, role_name) VALUES
    (1, 'Planner'),
    (2, 'Dispatcher'),
    (3, 'Fleet Manager');

INSERT INTO driver_statuses (status_code, description) VALUES
    ('Available', 'Ready for standard dispatch'),
    ('On Delivery', 'Assigned to an active container pickup'),
    ('Offline', 'Not available for dispatch');

INSERT INTO dispatch_statuses (status_code, description) VALUES
    ('Pending', 'Awaiting driver allocation'),
    ('Dispatched', 'Driver en route — port pickup then warehouse delivery'),
    ('At Port', 'Driver within port geofence (legacy)'),
    ('At Warehouse', 'Container delivered to de-stuff yard');

CREATE TABLE assignment_outcomes (
    outcome_code VARCHAR(20) PRIMARY KEY,
    description VARCHAR(100) NOT NULL
);

INSERT INTO assignment_outcomes (outcome_code, description) VALUES
    ('pending', 'Awaiting driver accept or reject'),
    ('accepted', 'Driver accepted the assignment'),
    ('rejected', 'Driver declined the assignment'),
    ('superseded', 'Replaced by a new driver assignment'),
    ('completed', 'Delivery confirmed via POD');

INSERT INTO tracking_statuses (status_code, description) VALUES
    ('At Sea', 'Vessel underway to port'),
    ('Approaching', 'Vessel entering port approach channel'),
    ('Berthing', 'Vessel berthing at terminal'),
    ('At Berth', 'Vessel moored at berth');

INSERT INTO ports (port_id, port_name, latitude, longitude, geofence_radius_km, max_prime_movers) VALUES
    ('TUAS-PSA', 'Tuas Port Terminal', 1.301500, 103.634000, 2.00, 3);

CREATE TABLE warehouses (
    warehouse_id VARCHAR(20) PRIMARY KEY,
    warehouse_name VARCHAR(100) NOT NULL,
    latitude DECIMAL(10, 6) NOT NULL,
    longitude DECIMAL(10, 6) NOT NULL,
    geofence_radius_km DECIMAL(4, 2) NOT NULL DEFAULT 1.50
);

INSERT INTO warehouses (warehouse_id, warehouse_name, latitude, longitude, geofence_radius_km) VALUES
    ('WH-JURONG', 'Jurong De-stuff Yard', 1.329800, 103.695400, 1.50);

-- ---------------------------------------------------------------------------
-- Vessel registry (stable identity) + voyages (trip instance) + AIS telemetry
-- ---------------------------------------------------------------------------
CREATE TABLE vessels (
    vessel_id VARCHAR(50) PRIMARY KEY,
    vessel_name VARCHAR(100) NOT NULL
);

CREATE TABLE voyages (
    voyage_id VARCHAR(50) PRIMARY KEY,
    vessel_id VARCHAR(50) NOT NULL,
    voyage_number VARCHAR(50) NOT NULL,
    FOREIGN KEY (vessel_id) REFERENCES vessels(vessel_id),
    INDEX idx_voyages_vessel (vessel_id)
);

CREATE TABLE vessel_tracking (
    voyage_id VARCHAR(50) PRIMARY KEY,
    latitude DECIMAL(10, 6) DEFAULT NULL,
    longitude DECIMAL(10, 6) DEFAULT NULL,
    heading DECIMAL(5, 1) DEFAULT 0,
    speed_knots DECIMAL(5, 1) DEFAULT 0,
    eta_datetime DATETIME DEFAULT NULL,
    tracking_status_code VARCHAR(20) NOT NULL DEFAULT 'At Sea',
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (voyage_id) REFERENCES voyages(voyage_id) ON DELETE CASCADE,
    FOREIGN KEY (tracking_status_code) REFERENCES tracking_statuses(status_code),
    INDEX idx_vessel_tracking_eta (eta_datetime, tracking_status_code)
);

-- ---------------------------------------------------------------------------
-- Containers (cargo facts only — dispatch state lives in truck_allocations)
-- ---------------------------------------------------------------------------
CREATE TABLE containers (
    container_number VARCHAR(11) PRIMARY KEY,
    voyage_id VARCHAR(50) NOT NULL,
    discharge_datetime DATETIME NOT NULL,
    lfd_datetime DATETIME NOT NULL,
    import_status VARCHAR(50) NOT NULL DEFAULT 'Imported',
    FOREIGN KEY (voyage_id) REFERENCES voyages(voyage_id),
    INDEX idx_containers_lfd (lfd_datetime),
    INDEX idx_containers_voyage (voyage_id)
);

-- ---------------------------------------------------------------------------
-- Audit / replay event stream
-- ---------------------------------------------------------------------------
CREATE TABLE events (
    event_id INT AUTO_INCREMENT PRIMARY KEY,
    container_number VARCHAR(11) DEFAULT NULL,
    source_api VARCHAR(50),
    event_type VARCHAR(50) NOT NULL,
    event_timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    raw_payload JSON,
    INDEX idx_events_container (container_number, event_timestamp),
    INDEX idx_events_type (event_type, event_timestamp)
);

-- ---------------------------------------------------------------------------
-- Drivers (identity + operational status) + GPS snapshot (telemetry)
-- ---------------------------------------------------------------------------
CREATE TABLE drivers (
    driver_id INT AUTO_INCREMENT PRIMARY KEY,
    driver_name VARCHAR(100) NOT NULL,
    phone_number VARCHAR(20) NOT NULL,
    status_code VARCHAR(20) NOT NULL DEFAULT 'Available',
    FOREIGN KEY (status_code) REFERENCES driver_statuses(status_code),
    INDEX idx_drivers_status (status_code)
);

CREATE TABLE driver_locations (
    driver_id INT PRIMARY KEY,
    latitude DECIMAL(10, 6) NOT NULL,
    longitude DECIMAL(10, 6) NOT NULL,
    heading DECIMAL(5, 1) DEFAULT 0,
    speed_kph DECIMAL(5, 1) DEFAULT 0,
    recorded_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (driver_id) REFERENCES drivers(driver_id) ON DELETE CASCADE
);

CREATE TABLE driver_schedules (
    schedule_id INT AUTO_INCREMENT PRIMARY KEY,
    driver_id INT NOT NULL,
    day_of_week TINYINT NOT NULL COMMENT '0=Monday through 6=Sunday',
    shift_start TIME NOT NULL,
    shift_end TIME NOT NULL,
    FOREIGN KEY (driver_id) REFERENCES drivers(driver_id) ON DELETE CASCADE,
    UNIQUE KEY unique_driver_day (driver_id, day_of_week)
);

-- ---------------------------------------------------------------------------
-- Dispatch allocations (links container ↔ driver; owns lifecycle timestamps)
-- ---------------------------------------------------------------------------
CREATE TABLE truck_allocations (
    allocation_id INT AUTO_INCREMENT PRIMARY KEY,
    container_number VARCHAR(11) NOT NULL,
    urgency_score INT DEFAULT 0,
    dispatch_status_code VARCHAR(20) NOT NULL DEFAULT 'Pending',
    warehouse_id VARCHAR(20) NOT NULL DEFAULT 'WH-JURONG',
    allocated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    accepted_at DATETIME DEFAULT NULL,
    picked_up_at DATETIME DEFAULT NULL,
    at_port_at DATETIME DEFAULT NULL,
    UNIQUE KEY unique_container (container_number),
    FOREIGN KEY (container_number) REFERENCES containers(container_number) ON DELETE CASCADE,
    FOREIGN KEY (dispatch_status_code) REFERENCES dispatch_statuses(status_code),
    FOREIGN KEY (warehouse_id) REFERENCES warehouses(warehouse_id),
    INDEX idx_allocations_status (dispatch_status_code)
);

-- ---------------------------------------------------------------------------
-- Dispatch assignment history (one row per driver-offer; 3NF outcome lookup)
-- ---------------------------------------------------------------------------
CREATE TABLE dispatch_assignments (
    assignment_id INT AUTO_INCREMENT PRIMARY KEY,
    allocation_id INT NULL,
    driver_id INT NOT NULL,
    assigned_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    outcome_code VARCHAR(20) NOT NULL DEFAULT 'pending',
    outcome_at DATETIME NULL,
    FOREIGN KEY (allocation_id) REFERENCES truck_allocations(allocation_id) ON DELETE SET NULL,
    FOREIGN KEY (driver_id) REFERENCES drivers(driver_id),
    FOREIGN KEY (outcome_code) REFERENCES assignment_outcomes(outcome_code),
    INDEX idx_assignments_allocation (allocation_id, assigned_at),
    INDEX idx_assignments_driver_outcome (driver_id, outcome_code)
);

-- ---------------------------------------------------------------------------
-- Driver job rejections (reason only; driver/container via assignment_id)
-- ---------------------------------------------------------------------------
CREATE TABLE job_rejections (
    rejection_id INT AUTO_INCREMENT PRIMARY KEY,
    assignment_id INT NOT NULL,
    reason VARCHAR(500) NOT NULL,
    rejected_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY unique_assignment_rejection (assignment_id),
    FOREIGN KEY (assignment_id) REFERENCES dispatch_assignments(assignment_id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- POD delivery completions (linked to accepted assignment; survives expunge)
-- ---------------------------------------------------------------------------
CREATE TABLE delivery_completions (
    completion_id INT AUTO_INCREMENT PRIMARY KEY,
    assignment_id INT NOT NULL,
    completed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    pod_note TEXT,
    pod_signature VARCHAR(500),
    confirmed_by_user_id INT NULL,
    UNIQUE KEY unique_assignment_completion (assignment_id),
    FOREIGN KEY (assignment_id) REFERENCES dispatch_assignments(assignment_id),
    FOREIGN KEY (confirmed_by_user_id) REFERENCES users(user_id) ON DELETE SET NULL
);

-- ---------------------------------------------------------------------------
-- Port prime-mover slot bookings (PSA gate capacity mock)
-- ---------------------------------------------------------------------------
CREATE TABLE port_slot_bookings (
    booking_id INT AUTO_INCREMENT PRIMARY KEY,
    port_id VARCHAR(20) NOT NULL,
    slot_number TINYINT UNSIGNED NOT NULL,
    allocation_id INT NOT NULL,
    booked_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    released_at DATETIME DEFAULT NULL,
    FOREIGN KEY (port_id) REFERENCES ports(port_id),
    FOREIGN KEY (allocation_id) REFERENCES truck_allocations(allocation_id) ON DELETE CASCADE,
    INDEX idx_slot_bookings_active (port_id, released_at),
    INDEX idx_slot_bookings_allocation (allocation_id, released_at)
);

-- ---------------------------------------------------------------------------
-- Escalation alerts (append-only log; container FK optional after expunge)
-- ---------------------------------------------------------------------------
CREATE TABLE alerts_log (
    alert_id INT AUTO_INCREMENT PRIMARY KEY,
    alert_type VARCHAR(50) NOT NULL,
    container_number VARCHAR(11) DEFAULT NULL,
    message TEXT,
    channel VARCHAR(50),
    sent_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_alerts_type (alert_type, sent_at)
);

-- ---------------------------------------------------------------------------
-- Web app users (credentials; role via FK not duplicated string)
-- ---------------------------------------------------------------------------
CREATE TABLE users (
    user_id INT AUTO_INCREMENT PRIMARY KEY,
    username VARCHAR(50) NOT NULL UNIQUE,
    email VARCHAR(100) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    role_id TINYINT UNSIGNED NOT NULL DEFAULT 1,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (role_id) REFERENCES roles(role_id)
);

-- ---------------------------------------------------------------------------
-- Staff leave requests and SQL validation triggers (Max 2 per type per day)
-- ---------------------------------------------------------------------------
CREATE TABLE leave_requests (
    leave_id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NULL COMMENT 'References users.user_id if Planner or Dispatcher',
    driver_id INT NULL COMMENT 'References drivers.driver_id if Driver',
    leave_date DATE NOT NULL,
    reason VARCHAR(255) NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
    FOREIGN KEY (driver_id) REFERENCES drivers(driver_id) ON DELETE CASCADE,
    UNIQUE KEY unique_employee_leave (user_id, driver_id, leave_date)
);

DELIMITER //
CREATE TRIGGER before_leave_request_insert
BEFORE INSERT ON leave_requests
FOR EACH ROW
BEGIN
    DECLARE leave_count INT;
    DECLARE user_role_id TINYINT;
    DECLARE total_users INT;

    IF NEW.driver_id IS NOT NULL THEN
        SELECT COUNT(*) INTO leave_count FROM leave_requests WHERE leave_date = NEW.leave_date AND driver_id IS NOT NULL;
        IF leave_count >= 2 THEN
            SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'Leave limit reached: A maximum of 2 drivers can be on leave on any given day.';
        END IF;
    ELSEIF NEW.user_id IS NOT NULL THEN
        SELECT role_id INTO user_role_id FROM users WHERE user_id = NEW.user_id;
        SELECT COUNT(*) INTO total_users FROM users WHERE role_id = user_role_id;
        SELECT COUNT(*) INTO leave_count
        FROM leave_requests lr
        JOIN users u ON u.user_id = lr.user_id
        WHERE lr.leave_date = NEW.leave_date 
          AND lr.driver_id IS NULL
          AND u.role_id = user_role_id;

        IF total_users - leave_count <= 1 THEN
            SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'Leave limit reached: At least 1 employee of this role must remain on duty.';
        END IF;
    END IF;
END //
DELIMITER ;

DELIMITER //
CREATE TRIGGER before_leave_request_update
BEFORE UPDATE ON leave_requests
FOR EACH ROW
BEGIN
    DECLARE leave_count INT;
    DECLARE user_role_id TINYINT;
    DECLARE total_users INT;

    IF NEW.leave_date != OLD.leave_date THEN
        IF NEW.driver_id IS NOT NULL THEN
            SELECT COUNT(*) INTO leave_count FROM leave_requests WHERE leave_date = NEW.leave_date AND driver_id IS NOT NULL;
            IF leave_count >= 2 THEN
                SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'Leave limit reached: A maximum of 2 drivers can be on leave on any given day.';
            END IF;
        ELSEIF NEW.user_id IS NOT NULL THEN
            SELECT role_id INTO user_role_id FROM users WHERE user_id = NEW.user_id;
            SELECT COUNT(*) INTO total_users FROM users WHERE role_id = user_role_id;
            SELECT COUNT(*) INTO leave_count
            FROM leave_requests lr
            JOIN users u ON u.user_id = lr.user_id
            WHERE lr.leave_date = NEW.leave_date 
              AND lr.driver_id IS NULL
              AND u.role_id = user_role_id;

            IF total_users - leave_count <= 1 THEN
                SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'Leave limit reached: At least 1 employee of this role must remain on duty.';
            END IF;
        END IF;
    END IF;
END //
DELIMITER ;

-- ---------------------------------------------------------------------------
-- Convenience views (read-only; optional for reporting tools)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_drivers_live AS
SELECT
    d.driver_id,
    d.driver_name,
    d.phone_number,
    d.status_code AS current_status,
    dl.latitude,
    dl.longitude,
    dl.heading,
    dl.speed_kph,
    dl.recorded_at AS last_gps_update
FROM drivers d
LEFT JOIN driver_locations dl ON dl.driver_id = d.driver_id;

CREATE OR REPLACE VIEW v_vessels_live AS
SELECT
    v.vessel_id,
    v.vessel_name,
    vy.voyage_id,
    vy.voyage_number,
    vt.latitude,
    vt.longitude,
    vt.heading,
    vt.speed_knots,
    vt.eta_datetime,
    vt.tracking_status_code AS tracking_status
FROM vessels v
JOIN voyages vy ON vy.vessel_id = v.vessel_id
LEFT JOIN vessel_tracking vt ON vt.voyage_id = vy.voyage_id;

CREATE OR REPLACE VIEW v_container_dispatch AS
SELECT
    c.container_number,
    c.lfd_datetime,
    c.discharge_datetime,
    c.import_status,
    vy.voyage_id,
    v.vessel_name,
    vy.voyage_number,
    t.allocation_id,
    da.driver_id,
    t.dispatch_status_code AS dispatch_status,
    t.allocated_at,
    t.accepted_at,
    t.at_port_at,
    d.driver_name,
    dl.latitude AS driver_lat,
    dl.longitude AS driver_lng,
    dl.speed_kph
FROM containers c
JOIN voyages vy ON vy.voyage_id = c.voyage_id
JOIN vessels v ON v.vessel_id = vy.vessel_id
LEFT JOIN truck_allocations t ON t.container_number = c.container_number
LEFT JOIN dispatch_assignments da ON da.allocation_id = t.allocation_id AND da.outcome_code IN ('accepted', 'completed')
LEFT JOIN drivers d ON d.driver_id = da.driver_id
LEFT JOIN driver_locations dl ON dl.driver_id = d.driver_id;

SET FOREIGN_KEY_CHECKS = 1;
