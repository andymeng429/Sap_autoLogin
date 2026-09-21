import platform
import sys
import win32com.client
def main():
    try:
        SapGuiAuto = win32com.client.GetObject("SAPGUI")
        if not type(SapGuiAuto) == win32com.client.CDispatch:
            print("Can not get SapGuiAuto")
            return

        application = SapGuiAuto.GetScriptingEngine
        if not type(application) == win32com.client.CDispatch:
            print("Can not get application")
            SapGuiAuto = None
            return
        application.HistoryEnabled = False
        connection = application.Children(0)
        if not type(connection) == win32com.client.CDispatch:
            print("Can not get connection")
            application = None
            SapGuiAuto = None
            return
        if connection.DisabledByServer == True:
            print("Scripting is disabled by server")
            connection = None
            application = None
            SapGuiAuto = None
            return
        session = connection.Children(0)
        if not type(session) == win32com.client.CDispatch:
            print("Can not get session")
            connection = None
            application = None
            SapGuiAuto = None
            return
        if session.Busy == True:
            print("Session is busy")
            session = None
            connection = None
            application = None
            SapGuiAuto = None
            return
        if session.Info.IsLowSpeedConnection == True:
            print("Connection is low speed")
            session = None
            connection = None
            application = None
            SapGuiAuto = None
            return
    except Exception as ex:
        print("Exception: " + str(ex))
        print(sys.exc_info()[0])
    finally:
        application.HistoryEnabled = True
        session = None
        connection = None
        application = None
        SapGuiAuto = None
# Main
if __name__ == "__main__":
    if platform.system() == "Windows":
        main()
# End-----