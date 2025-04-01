
class EX_ATTHOR(Exception):
    pass

class Raidqry():
    exceptions = { 251: EX_ATTHOR}
    def __init__(self,test):
        self.test = test

    
