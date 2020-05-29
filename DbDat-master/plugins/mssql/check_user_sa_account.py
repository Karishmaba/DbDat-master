class check_user_sa_account():
    """
    check_user_sa_account:
    Rename and disable the SA account if your applications allow it.
    """
    # References:
    # https://www.mssqltips.com/sqlservertip/2006/secure-and-disable-the-sql-server-sa-account/

    TITLE    = 'SA Account'
    CATEGORY = 'Configuration'
    TYPE     = 'sql'
    SQL         = "SELECT loginname FROM master..syslogins WHERE name='sa'"

    verbose = False
    skip    = False
    result  = {}

    def do_check(self, *results):
        self.result['level'] = 'GREEN'
        output               = 'sa account does not exist.'

        for rows in results:
            for row in rows:
                self.result['level'] = 'YELLOW'
                output = 'sa account does exist.'

        self.result['output'] = output

        return self.result

    def __init__(self, parent):
        print('Performing check: ' + self.TITLE)
