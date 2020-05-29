class check_privileges_public_role():
    """
    check_privileges_public_role:
    By default the Public role may unecessarily have rights to many objects
    in the database. Best practice is to follow the principle of least
    privileges.
    """
    # References:
    # https://www.mssqltips.com/sqlservertip/1694/how-to-find-out-what-sql-server-rights-have-been-granted-to-the-public-role/

    TITLE    = 'Public Role Privileges'
    CATEGORY = 'Privilege'
    TYPE     = 'sql'
    SQL         = "SELECT SDP.state_desc, SDP.permission_name, SSU.[name] AS \"Schema\", SSO.[name], SSO.[type] FROM sys.sysobjects SSO INNER JOIN sys.database_permissions SDP ON SSO.id = SDP.major_id INNER JOIN sys.sysusers SSU ON SSO.uid = SSU.uid ORDER BY SSU.[name], SSO.[name]"

    verbose = False
    skip    = False
    result  = {}

    def do_check(self, *results):
        self.result['level'] = 'GREEN'
        output = 'No privileges granted to public.'
        count  = 0

        for rows in results:
            for row in rows:
                count += 1

                if 1 == count:
                    self.result['level'] = 'YELLOW'
                    output = 'Privileges granted to public:\n'

                output += '%s %s %s\n' % (row[1], row[2], row[3])

        self.result['output'] = output

        return self.result

    def __init__(self, parent):
        print('Performing check: ' + self.TITLE)
