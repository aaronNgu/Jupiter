-- SQLite dump of a recording.db created by openadapt-capture's
-- SQLAlchemy Base.metadata.create_all() (the legacy capturer),
-- with one unsent recording/screenshot/window_event/action_event.
-- Regenerate per the comment in tests/test_legacy_db_compat.py.
BEGIN TRANSACTION;
CREATE TABLE action_event (
	id INTEGER NOT NULL, 
	name VARCHAR, 
	timestamp NUMERIC(10, 2), 
	recording_timestamp NUMERIC(10, 2), 
	recording_id INTEGER, 
	screenshot_timestamp NUMERIC(10, 2), 
	screenshot_id INTEGER, 
	window_event_timestamp NUMERIC(10, 2), 
	window_event_id INTEGER, 
	browser_event_timestamp NUMERIC(10, 2), 
	browser_event_id INTEGER, 
	mouse_x NUMERIC, 
	mouse_y NUMERIC, 
	mouse_dx NUMERIC, 
	mouse_dy NUMERIC, 
	active_segment_description VARCHAR, 
	available_segment_descriptions VARCHAR, 
	mouse_button_name VARCHAR, 
	mouse_pressed BOOLEAN, 
	key_name VARCHAR, 
	key_char VARCHAR, 
	key_vk VARCHAR, 
	canonical_key_name VARCHAR, 
	canonical_key_char VARCHAR, 
	canonical_key_vk VARCHAR, 
	parent_id INTEGER, 
	element_state JSON, 
	disabled BOOLEAN, 
	CONSTRAINT pk_action_event PRIMARY KEY (id), 
	CONSTRAINT fk_action_event_recording_id_recording FOREIGN KEY(recording_id) REFERENCES recording (id), 
	CONSTRAINT fk_action_event_screenshot_id_screenshot FOREIGN KEY(screenshot_id) REFERENCES screenshot (id), 
	CONSTRAINT fk_action_event_window_event_id_window_event FOREIGN KEY(window_event_id) REFERENCES window_event (id), 
	CONSTRAINT fk_action_event_browser_event_id_browser_event FOREIGN KEY(browser_event_id) REFERENCES browser_event (id), 
	CONSTRAINT fk_action_event_parent_id_action_event FOREIGN KEY(parent_id) REFERENCES action_event (id)
);
INSERT INTO "action_event" VALUES(1,'click',1750000001,1750000000,1,NULL,NULL,NULL,1,NULL,NULL,100,200,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL);
CREATE TABLE audio_info (
	id INTEGER NOT NULL, 
	timestamp NUMERIC(10, 2), 
	flac_data BLOB, 
	transcribed_text VARCHAR, 
	recording_timestamp NUMERIC(10, 2), 
	recording_id INTEGER, 
	sample_rate INTEGER, 
	words_with_timestamps TEXT, 
	CONSTRAINT pk_audio_info PRIMARY KEY (id), 
	CONSTRAINT fk_audio_info_recording_id_recording FOREIGN KEY(recording_id) REFERENCES recording (id)
);
CREATE TABLE browser_event (
	id INTEGER NOT NULL, 
	recording_timestamp NUMERIC(10, 2), 
	recording_id INTEGER, 
	message JSON, 
	timestamp NUMERIC(10, 2), 
	CONSTRAINT pk_browser_event PRIMARY KEY (id), 
	CONSTRAINT fk_browser_event_recording_id_recording FOREIGN KEY(recording_id) REFERENCES recording (id)
);
CREATE TABLE memory_stat (
	id INTEGER NOT NULL, 
	recording_timestamp INTEGER, 
	recording_id INTEGER, 
	memory_usage_bytes NUMERIC(10, 2), 
	timestamp NUMERIC(10, 2), 
	CONSTRAINT pk_memory_stat PRIMARY KEY (id), 
	CONSTRAINT fk_memory_stat_recording_id_recording FOREIGN KEY(recording_id) REFERENCES recording (id)
);
CREATE TABLE performance_stat (
	id INTEGER NOT NULL, 
	recording_timestamp NUMERIC(10, 2), 
	recording_id INTEGER, 
	event_type VARCHAR, 
	start_time INTEGER, 
	end_time INTEGER, 
	window_id VARCHAR, 
	CONSTRAINT pk_performance_stat PRIMARY KEY (id), 
	CONSTRAINT fk_performance_stat_recording_id_recording FOREIGN KEY(recording_id) REFERENCES recording (id)
);
CREATE TABLE recording (
	id INTEGER NOT NULL, 
	timestamp NUMERIC(10, 2), 
	monitor_width INTEGER, 
	monitor_height INTEGER, 
	double_click_interval_seconds NUMERIC, 
	double_click_distance_pixels NUMERIC, 
	platform VARCHAR, 
	task_description VARCHAR, 
	video_start_time NUMERIC(10, 2), 
	config JSON, 
	original_recording_id INTEGER, 
	CONSTRAINT pk_recording PRIMARY KEY (id), 
	CONSTRAINT fk_recording_original_recording_id_recording FOREIGN KEY(original_recording_id) REFERENCES recording (id)
);
INSERT INTO "recording" VALUES(1,1750000000,1920,1080,NULL,NULL,'win32','luminque-background',NULL,NULL,NULL);
CREATE TABLE screenshot (
	id INTEGER NOT NULL, 
	recording_timestamp NUMERIC(10, 2), 
	recording_id INTEGER, 
	timestamp NUMERIC(10, 2), 
	png_data BLOB, 
	png_diff_data BLOB, 
	png_diff_mask_data BLOB, 
	CONSTRAINT pk_screenshot PRIMARY KEY (id), 
	CONSTRAINT fk_screenshot_recording_id_recording FOREIGN KEY(recording_id) REFERENCES recording (id)
);
INSERT INTO "screenshot" VALUES(1,1750000000,1,1750000001,X'6C65676163792D706E672D6279746573',NULL,NULL);
CREATE TABLE window_event (
	id INTEGER NOT NULL, 
	recording_timestamp NUMERIC(10, 2), 
	recording_id INTEGER, 
	timestamp NUMERIC(10, 2), 
	state JSON, 
	title VARCHAR, 
	"left" INTEGER, 
	top INTEGER, 
	width INTEGER, 
	height INTEGER, 
	window_id VARCHAR, 
	CONSTRAINT pk_window_event PRIMARY KEY (id), 
	CONSTRAINT fk_window_event_recording_id_recording FOREIGN KEY(recording_id) REFERENCES recording (id)
);
INSERT INTO "window_event" VALUES(1,1750000000,1,1750000001,NULL,'Legacy Window',0,0,800,600,'42');
COMMIT;
