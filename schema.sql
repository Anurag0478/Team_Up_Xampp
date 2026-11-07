-- TeamUp schema for MySQL / MariaDB (Cloud Deployment).

CREATE TABLE users (
  id             INT AUTO_INCREMENT PRIMARY KEY,
  name           VARCHAR(120) NOT NULL,
  email          VARCHAR(190) NOT NULL,
  password_hash  VARCHAR(255) NOT NULL,
  gender         VARCHAR(10),
  grad_year      INT,
  branch         VARCHAR(80),
  github_url     VARCHAR(255),
  bio            TEXT,
  availability   VARCHAR(10) NOT NULL DEFAULT 'Flexible',
  hours_per_week INT DEFAULT 10,
  created_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_users_email UNIQUE (email),
  CONSTRAINT ck_users_gender CHECK (gender IN ('Male','Female','Other')),
  CONSTRAINT ck_users_year   CHECK (grad_year BETWEEN 2020 AND 2040),
  CONSTRAINT ck_users_avail  CHECK (availability IN ('Weekdays','Weekends','Flexible')),
  CONSTRAINT ck_users_hours  CHECK (hours_per_week BETWEEN 0 AND 80)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE skills (
  id       INT AUTO_INCREMENT PRIMARY KEY,
  name     VARCHAR(80) NOT NULL,
  category VARCHAR(40) NOT NULL,
  CONSTRAINT uq_skills_name UNIQUE (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE user_skills (
  user_id  INT NOT NULL,
  skill_id INT NOT NULL,
  level    INT NOT NULL DEFAULT 3,
  PRIMARY KEY (user_id, skill_id),
  CONSTRAINT fk_us_user  FOREIGN KEY (user_id)  REFERENCES users(id)  ON DELETE CASCADE,
  CONSTRAINT fk_us_skill FOREIGN KEY (skill_id) REFERENCES skills(id) ON DELETE CASCADE,
  CONSTRAINT ck_us_level CHECK (level BETWEEN 1 AND 5)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE INDEX idx_user_skills_skill ON user_skills(skill_id, level);
CREATE INDEX idx_users_filters     ON users(grad_year, gender, availability);

CREATE TABLE events (
  id            INT AUTO_INCREMENT PRIMARY KEY,
  title         VARCHAR(150) NOT NULL,
  organizer     VARCHAR(120),
  start_date    DATE,
  end_date      DATE,
  mode          VARCHAR(10),
  max_team_size INT DEFAULT 4,
  description   TEXT,
  CONSTRAINT ck_events_mode CHECK (mode IN ('Online','Offline','Hybrid'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE teams (
  id           INT AUTO_INCREMENT PRIMARY KEY,
  event_id     INT NOT NULL,
  name         VARCHAR(100) NOT NULL,
  description  TEXT,
  leader_id    INT NOT NULL,
  max_size     INT NOT NULL DEFAULT 4,
  current_size INT NOT NULL DEFAULT 0,
  status       VARCHAR(10) NOT NULL DEFAULT 'open',
  created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_team_event_name UNIQUE (event_id, name),
  CONSTRAINT fk_teams_event  FOREIGN KEY (event_id)  REFERENCES events(id) ON DELETE CASCADE,
  CONSTRAINT fk_teams_leader FOREIGN KEY (leader_id) REFERENCES users(id),
  CONSTRAINT ck_teams_size   CHECK (max_size BETWEEN 2 AND 10),
  CONSTRAINT ck_teams_status CHECK (status IN ('open','full','closed'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE team_members (
  team_id   INT NOT NULL,
  user_id   INT NOT NULL,
  event_id  INT NOT NULL,
  role      VARCHAR(10) NOT NULL DEFAULT 'member',
  joined_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (team_id, user_id),
  CONSTRAINT uq_event_user UNIQUE (event_id, user_id),
  CONSTRAINT fk_tm_team  FOREIGN KEY (team_id)  REFERENCES teams(id)  ON DELETE CASCADE,
  CONSTRAINT fk_tm_user  FOREIGN KEY (user_id)  REFERENCES users(id),
  CONSTRAINT fk_tm_event FOREIGN KEY (event_id) REFERENCES events(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE team_required_skills (
  team_id  INT NOT NULL,
  skill_id INT NOT NULL,
  PRIMARY KEY (team_id, skill_id),
  CONSTRAINT fk_trs_team  FOREIGN KEY (team_id)  REFERENCES teams(id)  ON DELETE CASCADE,
  CONSTRAINT fk_trs_skill FOREIGN KEY (skill_id) REFERENCES skills(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE join_requests (
  id          INT AUTO_INCREMENT PRIMARY KEY,
  team_id     INT NOT NULL,
  user_id     INT NOT NULL,
  kind        VARCHAR(10) NOT NULL DEFAULT 'request',
  message     VARCHAR(300),
  status      VARCHAR(10) NOT NULL DEFAULT 'pending',
  created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
  resolved_at DATETIME NULL,
  pending_key TINYINT GENERATED ALWAYS AS (CASE WHEN status = 'pending' THEN 1 ELSE NULL END) STORED,
  CONSTRAINT uq_pending_request UNIQUE (team_id, user_id, pending_key),
  CONSTRAINT fk_jr_team FOREIGN KEY (team_id) REFERENCES teams(id) ON DELETE CASCADE,
  CONSTRAINT fk_jr_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
  CONSTRAINT ck_jr_kind   CHECK (kind IN ('request','invite')),
  CONSTRAINT ck_jr_status CHECK (status IN ('pending','accepted','rejected','withdrawn','closed'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

DELIMITER $$

CREATE TRIGGER trg_block_full_team BEFORE INSERT ON team_members
FOR EACH ROW
BEGIN
  IF (SELECT current_size >= max_size FROM teams WHERE id = NEW.team_id) THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Team is already full';
  END IF;
END$$

CREATE TRIGGER trg_member_added AFTER INSERT ON team_members
FOR EACH ROW
BEGIN
  UPDATE teams
     SET status = CASE WHEN current_size + 1 >= max_size THEN 'full' ELSE status END,
         current_size = current_size + 1
   WHERE id = NEW.team_id;
END$$

CREATE TRIGGER trg_member_removed AFTER DELETE ON team_members
FOR EACH ROW
BEGIN
  UPDATE teams
     SET current_size = current_size - 1,
         status = CASE WHEN status = 'full' THEN 'open' ELSE status END
   WHERE id = OLD.team_id;
END$$

DELIMITER ;

CREATE VIEW v_skill_gap AS
SELECT s.id, s.name, s.category,
  (SELECT COUNT(*) FROM team_required_skills r JOIN teams t ON t.id = r.team_id
    WHERE r.skill_id = s.id AND t.status = 'open')            AS demand,
  (SELECT COUNT(*) FROM user_skills us
    WHERE us.skill_id = s.id AND us.level >= 3)               AS supply
FROM skills s;