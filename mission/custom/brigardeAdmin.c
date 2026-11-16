class BrigardeBridgeConfig
{
    int protocol;
    string baseUrl;
    string token;
}

class BrigardePlayerSnapshot
{
    string id;
    string name;
    float x;
    float y;
    float z;
    bool alive;
    bool inVehicle;
}

class BrigardeCarSnapshot
{
    string id;
    string typeName;
    float x;
    float y;
    float z;
    bool occupied;
}

class BrigardePollPacket
{
    int protocol = 1;
    string token;
    string session;
    string world;
    int worldSize;
    string receiptId;
    bool receiptOk;
    string receiptDetail;
    ref array<ref BrigardePlayerSnapshot> players = new array<ref BrigardePlayerSnapshot>();
    ref array<ref BrigardeCarSnapshot> vehicles = new array<ref BrigardeCarSnapshot>();
}

class BrigardeBridgeCommand
{
    int protocol;
    string id;
    string action;
    string target;
    string typeName;
    int quantity;
    float x;
    float z;
    float heading;
    string message;
}

class BrigardeTestCar
{
    string id;
    CarScript car;
}

class BrigardeAdminCallback : RestCallback
{
    protected BrigardeAdminBridge m_Bridge;
    protected int m_Attempt;

    void BrigardeAdminCallback(BrigardeAdminBridge bridge, int attempt)
    {
        m_Bridge = bridge;
        m_Attempt = attempt;
    }

    override void OnSuccess(string data, int dataSize)
    {
        if (m_Bridge)
            m_Bridge.Receive(m_Attempt, data);
    }

    override void OnError(int errorCode)
    {
        if (m_Bridge)
            m_Bridge.RequestFailed(m_Attempt, "REST error " + errorCode.ToString());
    }

    override void OnTimeout()
    {
        if (m_Bridge)
            m_Bridge.RequestFailed(m_Attempt, "REST timeout");
    }
}

class BrigardeAdminBridge
{
    protected static const string CONFIG_PATH = "$mission:custom/BrigardeInitPanel.json";
    protected static const int POLL_MS = 2500;
    protected static const int CONFIG_MS = 10000;
    protected static const int REQUEST_DEADLINE_MS = 25000;
    protected static const int MAX_CARS = 24;

    protected ref BrigardeBridgeConfig m_Config;
    protected ref BrigardeAdminCallback m_Callback;
    protected ref array<ref BrigardeTestCar> m_Cars = new array<ref BrigardeTestCar>();
    protected ref TStringArray m_ItemNames = {
        "BandageDressing", "BakedBeansCan", "SodaCan_Pipsi", "WaterBottle",
        "M4A1", "AKM", "Mag_STANAG_30Rnd", "Mag_AKM_30Rnd"
    };
    protected ref TStringArray m_CarNames = {
        "OffroadHatchback", "CivilianSedan", "Hatchback_02", "Sedan_02"
    };

    protected string m_Session;
    protected string m_World;
    protected int m_WorldSize;
    protected int m_ConfigDue;
    protected int m_Attempt;
    protected int m_RequestStarted;
    protected int m_LastCar;
    protected bool m_Running;
    protected bool m_InFlight;
    protected string m_LastStatus;
    protected string m_ReceiptId;
    protected bool m_ReceiptOk;
    protected string m_ReceiptDetail;

    void Start()
    {
        if (m_Running)
            return;
        m_Running = true;
        int left = Math.RandomInt(100000000, 999999999);
        int right = Math.RandomInt(100000000, 999999999);
        m_Session = "dayz-" + left.ToString() + "-" + right.ToString();
        Print("[Brigarde Init Panel] Mission script loaded.");
        GetGame().GetCallQueue(CALL_CATEGORY_SYSTEM).CallLater(this.Poll, POLL_MS, true);
    }

    void Stop()
    {
        if (!m_Running)
            return;
        m_Running = false;
        m_InFlight = false;
        GetGame().GetCallQueue(CALL_CATEGORY_SYSTEM).Remove(this.Poll);
    }

    protected void Status(string message)
    {
        if (m_LastStatus == message)
            return;
        m_LastStatus = message;
        Print("[Brigarde Init Panel] " + message);
    }

    protected void LoadConfig()
    {
        m_Config = null;
        if (!FileExist(CONFIG_PATH))
        {
            Status("Waiting for custom/BrigardeInitPanel.json.");
            return;
        }
        BrigardeBridgeConfig candidate = new BrigardeBridgeConfig();
        string readError;
        if (!JsonFileLoader<BrigardeBridgeConfig>.LoadFile(CONFIG_PATH, candidate, readError) || !candidate)
        {
            Status("The connection config could not be read.");
            return;
        }
        bool validScheme = candidate.baseUrl.IndexOf("https://") == 0
            || candidate.baseUrl.IndexOf("http://127.0.0.1:") == 0;
        if (candidate.protocol != 1 || candidate.token.Length() != 43 || !validScheme
            || candidate.baseUrl.Length() > 300 || candidate.baseUrl.IndexOf("@") >= 0
            || candidate.baseUrl.IndexOf("?") >= 0 || candidate.baseUrl.IndexOf("#") >= 0)
        {
            Status("The connection config is invalid. Export it from the local panel.");
            return;
        }
        m_Config = candidate;
    }

    protected void Poll()
    {
        if (!m_Running)
            return;
        int now = GetGame().GetTime();
        if (m_InFlight)
        {
            if (now - m_RequestStarted >= REQUEST_DEADLINE_MS)
                RequestFailed(m_Attempt, "REST request reached its deadline");
            return;
        }
        if (m_WorldSize <= 0 || m_World == "")
        {
            World world = GetGame().GetWorld();
            if (!world)
                return;
            m_World = GetGame().GetWorldName();
            m_WorldSize = world.GetWorldSize();
            if (m_World == "" || m_WorldSize <= 0)
                return;
        }
        if (!m_ConfigDue || now >= m_ConfigDue)
        {
            m_ConfigDue = now + CONFIG_MS;
            LoadConfig();
        }
        if (!m_Config)
            return;

        RestApi api = GetRestApi();
        if (!api)
            api = CreateRestApi();
        if (!api)
        {
            Status("REST is unavailable in this mission.");
            return;
        }
        RestContext context = api.GetRestContext(m_Config.baseUrl);
        if (!context)
        {
            Status("The REST context could not be created.");
            return;
        }

        BrigardePollPacket packet = new BrigardePollPacket();
        packet.token = m_Config.token;
        packet.session = m_Session;
        packet.world = m_World;
        packet.worldSize = m_WorldSize;
        packet.receiptId = m_ReceiptId;
        packet.receiptOk = m_ReceiptOk;
        packet.receiptDetail = m_ReceiptDetail;
        CollectPlayers(packet.players);
        CollectCars(packet.vehicles);

        string payload;
        string writeError;
        if (!JsonFileLoader<BrigardePollPacket>.MakeData(packet, payload, writeError, false))
        {
            Status("The poll request could not be serialized.");
            return;
        }
        context.SetHeader("application/json");
        m_Attempt++;
        m_Callback = new BrigardeAdminCallback(this, m_Attempt);
        m_InFlight = true;
        m_RequestStarted = now;
        int result = context.POST(m_Callback, "/dayz/poll", payload);
        if (result >= ERestResultState.EREST_ERROR)
            RequestFailed(m_Attempt, "REST request could not start");
    }

    void RequestFailed(int attempt, string message)
    {
        if (!m_Running || !m_InFlight || attempt != m_Attempt)
            return;
        m_InFlight = false;
        Status(message);
    }

    void Receive(int attempt, string data)
    {
        if (!m_Running || !m_InFlight || attempt != m_Attempt)
            return;
        m_InFlight = false;
        if (data.Length() > 8192)
        {
            Status("The bridge response is too large.");
            return;
        }
        BrigardeBridgeCommand command = new BrigardeBridgeCommand();
        string readError;
        if (!JsonFileLoader<BrigardeBridgeCommand>.LoadData(data, command, readError)
            || !command || command.protocol != 1)
        {
            Status("The bridge response is invalid.");
            return;
        }
        Status("Connected to the panel.");
        if (command.id.Length() != 24 || command.id == m_ReceiptId)
            return;
        string detail;
        m_ReceiptOk = Execute(command, detail);
        m_ReceiptDetail = detail;
        m_ReceiptId = command.id;
    }

    protected void CollectPlayers(array<ref BrigardePlayerSnapshot> output)
    {
        array<Man> online = new array<Man>();
        GetGame().GetPlayers(online);
        foreach (Man human : online)
        {
            PlayerBase player = PlayerBase.Cast(human);
            if (!player)
                continue;
            PlayerIdentity identity = player.GetIdentity();
            if (!identity || identity.GetId() == "")
                continue;
            vector pos = player.GetPosition();
            BrigardePlayerSnapshot row = new BrigardePlayerSnapshot();
            row.id = identity.GetId();
            row.name = identity.GetName();
            if (row.name == "")
                row.name = "Player";
            row.x = pos[0];
            row.y = pos[1];
            row.z = pos[2];
            row.alive = player.IsAlive();
            row.inVehicle = player.GetCommand_Vehicle() != null;
            output.Insert(row);
        }
    }

    protected bool IsOccupied(CarScript vehicle)
    {
        for (int seat = 0; seat < vehicle.CrewSize(); seat++)
        {
            if (vehicle.CrewMember(seat))
                return true;
        }
        return false;
    }

    protected void CollectCars(array<ref BrigardeCarSnapshot> output)
    {
        for (int index = m_Cars.Count() - 1; index >= 0; index--)
        {
            BrigardeTestCar tracked = m_Cars[index];
            if (!tracked.car)
            {
                m_Cars.Remove(index);
                continue;
            }
            vector pos = tracked.car.GetPosition();
            BrigardeCarSnapshot row = new BrigardeCarSnapshot();
            row.id = tracked.id;
            row.typeName = tracked.car.GetType();
            row.x = pos[0];
            row.y = pos[1];
            row.z = pos[2];
            row.occupied = IsOccupied(tracked.car);
            output.Insert(row);
        }
    }

    protected PlayerBase FindPlayer(string target)
    {
        if (target == "")
            return null;
        array<Man> online = new array<Man>();
        GetGame().GetPlayers(online);
        foreach (Man human : online)
        {
            PlayerBase player = PlayerBase.Cast(human);
            if (player && player.IsAlive() && player.GetIdentity() && player.GetIdentity().GetId() == target)
                return player;
        }
        return null;
    }

    protected bool GroundPosition(float x, float z, out vector destination)
    {
        if (x != x || z != z || x < 0 || z < 0 || x >= m_WorldSize || z >= m_WorldSize)
            return false;
        if (GetGame().SurfaceIsSea(x, z) || GetGame().SurfaceIsPond(x, z))
            return false;
        destination = Vector(x, GetGame().SurfaceY(x, z) + 0.25, z);
        return true;
    }

    protected bool Execute(BrigardeBridgeCommand command, out string detail)
    {
        if (command.action == "ping")
        {
            detail = "pong";
            return true;
        }
        if (command.action == "spawn_car")
            return SpawnCar(command, detail);
        if (command.action == "remove_car")
            return RemoveCar(command.target, detail);
        if (command.action != "notify" && command.action != "give" && command.action != "teleport")
        {
            detail = "unsupported";
            return false;
        }

        PlayerBase player = FindPlayer(command.target);
        if (!player)
        {
            detail = "player_missing";
            return false;
        }
        if (command.action == "give")
            return GiveItems(player, command, detail);
        if (command.action == "notify")
        {
            if (command.message == "" || command.message.Length() > 512)
            {
                detail = "invalid_message";
                return false;
            }
            NotificationSystem.SendNotificationToPlayerExtended(player, 8, "Brigarde Admin", command.message);
            detail = "notified";
            return true;
        }

        if (player.GetCommand_Vehicle() || player.GetCommand_Ladder())
        {
            detail = "player_busy";
            return false;
        }
        vector destination;
        if (!GroundPosition(command.x, command.z, destination))
        {
            detail = "invalid_ground";
            return false;
        }
        player.SetPosition(destination);
        detail = "teleported";
        return true;
    }

    protected bool GiveItems(PlayerBase player, BrigardeBridgeCommand command, out string detail)
    {
        if (m_ItemNames.Find(command.typeName) == -1 || command.quantity < 1 || command.quantity > 8)
        {
            detail = "invalid_item";
            return false;
        }
        int created = 0;
        while (created < command.quantity)
        {
            EntityAI item = player.GetInventory().CreateInInventory(command.typeName);
            if (!item)
                break;
            created++;
        }
        if (created != command.quantity)
        {
            detail = "item_partial:" + created.ToString();
            return false;
        }
        detail = "items_given:" + created.ToString();
        return true;
    }

    protected bool SpawnCar(BrigardeBridgeCommand command, out string detail)
    {
        if (m_CarNames.Find(command.typeName) == -1 || m_Cars.Count() >= MAX_CARS
            || command.heading != command.heading || command.heading < 0 || command.heading >= 360)
        {
            detail = "invalid_vehicle";
            return false;
        }
        vector destination;
        if (!GroundPosition(command.x, command.z, destination))
        {
            detail = "invalid_ground";
            return false;
        }
        array<Object> nearby = new array<Object>();
        array<CargoBase> cargo = new array<CargoBase>();
        GetGame().GetObjectsAtPosition3D(destination, 5, nearby, cargo);
        foreach (Object candidate : nearby)
        {
            if (Transport.Cast(candidate) || Man.Cast(candidate))
            {
                detail = "spawn_occupied";
                return false;
            }
        }
        int flags = ECE_SETUP | ECE_PLACE_ON_SURFACE | ECE_NOPERSISTENCY_WORLD;
        Object created = GetGame().CreateObjectEx(command.typeName, destination, flags);
        CarScript vehicle = CarScript.Cast(created);
        if (!vehicle)
        {
            if (created)
                GetGame().ObjectDelete(created);
            detail = "spawn_failed";
            return false;
        }
        vehicle.SetOrientation(Vector(command.heading, 0, 0));
        vehicle.OnDebugSpawn();
        m_LastCar++;
        BrigardeTestCar tracked = new BrigardeTestCar();
        tracked.id = "car-" + m_LastCar.ToString();
        tracked.car = vehicle;
        m_Cars.Insert(tracked);
        detail = "car_spawned:" + tracked.id;
        return true;
    }

    protected bool RemoveCar(string target, out string detail)
    {
        for (int index = 0; index < m_Cars.Count(); index++)
        {
            BrigardeTestCar tracked = m_Cars[index];
            if (tracked.id != target || !tracked.car)
                continue;
            if (IsOccupied(tracked.car))
            {
                detail = "vehicle_occupied";
                return false;
            }
            GetGame().ObjectDelete(tracked.car);
            m_Cars.Remove(index);
            detail = "car_removed";
            return true;
        }
        detail = "vehicle_missing";
        return false;
    }
}
