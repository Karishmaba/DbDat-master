class check_privilege_load():
    """
    check_privilege_load:
    The IMPLSCHEMA (implicit schema) role grants the authority to a user to create objects
    without specifying a schema that already exists. It is recommended that the implschema
    role be granted to authorized user.
    """
    # References:
    # https://benchmarks.cisecurity.org/downloads/show-single/?file=db2.120

    TITLE    = 'LOADAUTH Role'
    CATEGORY = 'Privilege'
    TYPE     = 'sql'
    SQL         = "SELECT DISTINCT grantee, granteetype FROM syscat.dbauth WHERE loadauth='Y'"

    verbose = False
    skip    = False
    result  = {}

    def do_check(self, *results):
        output = ''
        self.result['level']  = 'GREEN'

        for rows in results:
            for row in rows:
                self.result['level']  = 'YELLOW'
                output += 'LOADAUTH granted to %s\n' % (row[0])

        if 'GREEN' == self.result['level']:
            output = 'No users granted LOADAUTH.'

        self.result['output'] = output

        return self.result

    def __init__(self, parent):
        print('Performing check: ' + self.TITLE)
