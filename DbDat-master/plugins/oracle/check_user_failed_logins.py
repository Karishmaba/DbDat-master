class check_user_failed_logins():
    """
    check_user_failed_logins:
    Check the number of allowed failed login attempts. The failed_login_attempts
    setting determines how many    failed login attempts are permitted before the
    system locks the user's account. While different profiles can have different
    and more restrictive settings, such as USERS and APPS, the minimum(s)
    recommended    here should    be set on the DEFAULT profile.
    """
    # References:
    # https://benchmarks.cisecurity.org/downloads/show-single/?file=oracle11gR2.210

    TITLE    = 'Failed Login Attempts'
    CATEGORY = 'Configuration'
    TYPE     = 'sql'
    SQL         = "SELECT profile, resource_name, limit FROM sys.dba_profiles WHERE resource_name='FAILED_LOGIN_ATTEMPTS' AND profile IN (SELECT profile FROM sys.dba_users) ORDER BY profile"

    verbose = False
    skip    = False
    result  = {}

    def do_check(self, *results):
        output       = ''

        for rows in results:
            for row in rows:
                if 'UNLIMITED' == row[2]:
                    self.result['level'] = 'RED'
                    output += 'Number of allowed failed attempts for the ' + row[0] + ' profile is ' + row[2] + '\n'
                elif row[2] > 10:
                    self.result['level'] = 'RED'
                    output += 'Number of allowed failed attempts for the ' + row[0] + ' profile is ' + row[2] + '\n'
                elif row[2] > 3:
                    self.result['level'] = 'YELLOW'
                    output += 'Number of allowed failed attempts for the ' + row[0] + ' profile is ' + row[2] + '\n'
                else:
                    self.result['level'] = 'GREEN'
                    output += 'Number of allowed failed attempts for the ' + row[0] + ' profile is ' + row[2] + '\n'

        self.result['output'] = output

        return self.result

    def __init__(self, parent):
        print('Performing check: ' + self.TITLE)
