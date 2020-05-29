from pymongo import MongoClient
from plugins.mongodb.helper import get_yaml_config_value

class check_configuration_verify_auth():
    """
    check_configuration_verify_auth:
    Verifying authentication is required.
    """
    # References:
    # https://docs.mongodb.org/v2.4/tutorial/enable-authentication/
    # https://docs.mongodb.org/v2.6/tutorial/enable-authentication/
    # https://docs.mongodb.org/v2.6/core/authentication/#localhost-exception

    TITLE    = 'Authentication Required'
    CATEGORY = 'Configuration'
    TYPE     = 'nosql'
    SQL         = None # SQL not needed... because this is NoSQL.

    verbose = False
    skip    = False
    result  = {}

    db      = None

    def do_check(self):
        try:
            dblist                = self.db.database_names()
            self.result['level']  = 'RED'
            self.result['output'] = 'Unauthenticated connection succeded, database list:\n%s\n' % (dblist)

        except Exception as e:
            self.result['level']  = 'GREEN'
            self.result['output'] = 'Unauthenticated connection failed, message:\n%s\n' % (e)

        self.db.close()

        return self.result

    def __init__(self, parent):
        print('Performing check: ' + self.TITLE)

        # parent connection is authenticated so create a new unauthenticated connection
        self.db = MongoClient(parent.dbhost, int(parent.dbport))
