class check_configuration_trace_files_public():
    """
    check_configuration_trace_files_public:
    The _trace_files_public setting determines whether or not the system's trace
    file is world readable.
    """
    # References:
    # https://benchmarks.cisecurity.org/downloads/show-single/?file=oracle11gR2.210

    TITLE    = 'Trace Files Public'
    CATEGORY = 'Configuration'
    TYPE     = 'sql'
    SQL         = "SELECT UPPER(value) FROM v$parameter WHERE UPPER(name)='_TRACE_FILES_PUBLIC'"

    verbose = False
    skip    = False
    result  = {}

    def do_check(self, *results):
        self.result['level']  = 'GREEN'
        self.result['output'] = '_TRACE_FILES_PUBLIC is (not set) not enabled.'

        for rows in results:
            for row in rows:
                if 'FALSE' != row[0]:
                    self.result['level']  = 'RED'
                    self.result['output'] = '_TRACE_FILES_PUBLIC is (%s) enabled.' % (row[0])
                else:
                    self.result['level']  = 'GREEN'
                    self.result['output'] = '_TRACE_FILES_PUBLIC is (%s) not enabled.' % (row[0])

        return self.result

    def __init__(self, parent):
        print('Performing check: ' + self.TITLE)
