update audit_evt 
set fk_status = 'SB',
    tx_hash = ?,
    status_info = ?,
    report = ?
where id = ?