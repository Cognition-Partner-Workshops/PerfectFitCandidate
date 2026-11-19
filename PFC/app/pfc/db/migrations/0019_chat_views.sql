CREATE VIEW chat_v_demands AS
SELECT d.code_requisition AS requisition,d.account_name AS account,d.bu,d.region,
 d.code_local_grade AS grade,d.position_start_date AS start_date,d.lifecycle
FROM demands d WHERE d.lifecycle='open';

CREATE VIEW chat_v_demand_skills AS
SELECT d.code_requisition AS requisition,s.name AS skill,i.kind
FROM demands d JOIN jd_requirements q ON q.demand_version_id=d.current_version_id
JOIN jd_requirement_items i USING(jd_req_id) JOIN skills s ON s.skill_id=i.canonical_skill_id
WHERE d.lifecycle='open' AND q.status='confirmed' AND s.is_active=1
 AND i.kind IN ('mandatory_skill','preferred_skill');

CREATE VIEW chat_v_resources AS
SELECT 'Resource #'||resource_id AS resource_ref,grade,location,bench_status_code AS bench_status,
 bench_start_date,bench_end_date,CAST(available_capacity AS REAL) AS capacity,eligibility,lifecycle
FROM resources WHERE lifecycle!='archived';

CREATE VIEW chat_v_resource_skills AS
SELECT 'Resource #'||r.resource_id AS resource_ref,s.name AS skill,rs.source,rs.years
FROM resources r JOIN resource_skills rs USING(resource_id)
JOIN skills s ON s.skill_id=rs.canonical_skill_id
WHERE r.lifecycle!='archived' AND s.is_active=1;

CREATE VIEW chat_v_match_results AS
SELECT d.code_requisition AS requisition,'Resource #'||r.resource_id AS resource_ref,
 m.rank,m.score,m.band,m.mandatory_missing,m.shortlisted
FROM match_results m JOIN demands d USING(demand_id) JOIN resources r USING(resource_id)
WHERE d.lifecycle='open' AND r.lifecycle!='archived'
AND m.run_id=(SELECT max(assessed.run_id) FROM (
 SELECT m.run_id,m.demand_id FROM match_results m JOIN match_runs r USING(run_id)
 WHERE r.status='completed'
 UNION
 SELECT r.run_id,CAST(j.key AS INTEGER) FROM match_runs r,
 json_each(CASE WHEN json_valid(r.dq_summary_json) THEN
 CASE WHEN json_type(r.dq_summary_json,'$.demands')='object'
 THEN json_extract(r.dq_summary_json,'$.demands') ELSE '{}' END ELSE '{}' END) j
 WHERE r.status='completed' AND trim(j.key)!=''
 AND ltrim(trim(j.key),'+-')!=''
 AND ltrim(trim(j.key),'+-') NOT GLOB '*[^0-9]*'
) assessed WHERE assessed.demand_id=m.demand_id);
