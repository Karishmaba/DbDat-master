class check_configuration_cors():
    """
    check_configuration_cors:
    CORS allows constrained access to CouchDB DBs and instances when accessed
    by a web browser.
    """
    # References:
    # https://wiki.apache.org/couchdb/CORS

    TITLE    = 'Enable CORS'
    CATEGORY = 'Configuration'
    TYPE     = 'nosql'
    SQL         = None # SQL not needed... because this is NoSQL.

    verbose = False
    skip    = False
    result  = {}
    db      = None

    def do_check(self):
        option = 'enable_cors'
        value  = self.db.config()['httpd'][option]

        if 'false' == value:
            self.result['level']  = 'RED'
            self.result['output'] = '%s is (%s) not enabled.' % (option, value)
        else:
            self.result['level']  = 'GREEN'
            self.result['output'] = '%s is (%s) enabled.' % (option, value)

        return self.result

    def __init__(self, parent):
        print('Performing check: ' + self.TITLE)
        self.db = parent.db
