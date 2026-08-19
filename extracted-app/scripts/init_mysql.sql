-- 在远程 MySQL 上执行一次（需 CREATE DATABASE 权限）
CREATE DATABASE IF NOT EXISTS oracle_recovery
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_unicode_ci;

CREATE USER IF NOT EXISTS 'recovery'@'%' IDENTIFIED BY 'recovery';
GRANT ALL PRIVILEGES ON oracle_recovery.* TO 'recovery'@'%';
FLUSH PRIVILEGES;
