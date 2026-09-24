-- Öffentliche Sicht auf Freds Archiv für Grafana (Nutzer grafana_ro).
-- Ohne gelöschte Nachrichten, ohne Team-/Log-/Ticket-Kanäle, ohne private Willkommens-Threads,
-- Text gekürzt. ALGORITHM=MERGE, damit die Indizes von fred_archiv (u. a. zeit) greifen.
CREATE OR REPLACE ALGORITHM=MERGE SQL SECURITY DEFINER VIEW DdR.fred_archiv_oeffentlich AS
SELECT nachricht_id, kanal_id, kanal, eltern_id, user_id, name, bot, zeit,
       LEFT(text, 300) AS text, anhaenge, geloescht
FROM DdR.fred_archiv
WHERE geloescht IS NULL
  AND kanal_id NOT IN (1060013640838828073, 1367480744280326266, 1548478201745899570,
                       1060013463205851236, 1455907960575230105, 1060004521838923786)
  AND COALESCE(eltern_id, 0) NOT IN (1060013640838828073, 1367480744280326266, 1548478201745899570,
                                     1060013463205851236, 1455907960575230105, 1060004521838923786)
  AND kanal NOT LIKE 'Willkommen %'
  AND kanal NOT REGEXP '(team|audit|logs|level-log|ticket|admin|mod-|moderation)';

REVOKE SELECT ON DdR.fred_archiv FROM 'grafana_ro'@'%';
GRANT SELECT ON DdR.fred_archiv_oeffentlich TO 'grafana_ro'@'%';
REVOKE SELECT ON DdR.fred_verdacht FROM 'grafana_ro'@'%';
