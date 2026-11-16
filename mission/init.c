#include "$mission:custom/brigardeAdmin.c"

void main()
{
    Hive economy = CreateHive();
    if (economy)
        economy.InitOffline();
}

class BrigardeTestMission : MissionServer
{
    protected ref BrigardeAdminBridge m_BrigardeAdmin;

    override void OnInit()
    {
        super.OnInit();
        if (GetGame().IsDedicatedServer())
        {
            m_BrigardeAdmin = new BrigardeAdminBridge();
            m_BrigardeAdmin.Start();
        }
    }

    override void OnMissionFinish()
    {
        if (m_BrigardeAdmin)
            m_BrigardeAdmin.Stop();
        super.OnMissionFinish();
    }
}

Mission CreateCustomMission(string path)
{
    return new BrigardeTestMission();
}
