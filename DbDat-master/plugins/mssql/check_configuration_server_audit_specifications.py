class check_configuration_server_audit_specifications():
    """
    check_configuration_server_audit_specifications:
    Auditing of defined server level events. Review to ensure proper auditing
    is enabled.
    """
    # References:
    # https://benchmarks.cisecurity.org/downloads/show-single/index.cfm?file=sql2012DB.120

    TITLE    = 'Server Audit Specifications'
    CATEGORY = 'Configuration'
    TYPE     = 'sql'
    SQL      = """
                SELECT audit_id, 
                a.name as audit_name, 
                s.name as server_specification_name,
                d.audit_action_name,
                s.is_state_enabled,
                d.is_group,
                d.audit_action_id,	
                s.create_date,
                s.modify_date
                FROM sys.server_audits AS a
                JOIN sys.server_audit_specifications AS s
                ON a.audit_guid = s.audit_guid
                JOIN sys.server_audit_specification_details AS d
                ON s.server_specification_id = d.server_specification_id
                WHERE s.is_state_enabled = 1
                """

    verbose = False
    skip    = False
    result  = {}

    def do_check(self, *results):
        
        for rows in results:
            if 0 == len(rows):
                self.result['level']  = 'RED'
                self.result['output'] = 'There are no server audit specifications enabled.'
                
            else:
               for row in rows:
                   self.result['level']  = 'YELLOW'
                   self.result['output'] = '%s %s %s %s \n' % (row[0], row[1], row[2], row[3])

        return self.result

    def __init__(self, parent):
        print('Performing check: ' + self.TITLE)
