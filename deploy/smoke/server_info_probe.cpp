#include "config.h"
#include "nNetwork.h"
#include "nServerInfo.h"
#include "nSocket.h"
#include "tDirectories.h"
#include "tLocale.h"
#include "tString.h"
#include "tSysTime.h"

#include <cstdlib>
#include <iostream>
#include <unistd.h>

bool *sg_GetSpecs()
{
    static bool spectators[MAXCLIENTS + 2] = {};
    return spectators;
}

extern nDescriptor RequestSmallServerInfoDescriptor;

int main( int argc, char **argv )
{
    if ( argc != 3 )
        return 2;

    char const *dataDirectory = std::getenv( "TRONNER_ENGINE_DATA_DIR" );
    if ( !dataDirectory || !*dataDirectory )
        return 4;
    tDirectories::SetData( tString( dataDirectory ) );
    tLocale::Load( "languages.txt" );

    unsigned int port = static_cast<unsigned int>( atoi( argv[2] ) );
    sn_SetNetState( nCLIENT );
    sn_Bend( tString( argv[1] ), port );
    tJUST_CONTROLLED_PTR< nMessage > request =
        tNEW(nMessage)( RequestSmallServerInfoDescriptor );
    request->ClearMessageID();
    request->SendImmediately( 0, false );
    nMessage::SendCollected( 0 );
    for ( int tick = 0;
          tick < 2000 && !nServerInfo::GetFirstServer(); ++tick )
    {
        sn_Receive();
        sn_SendPlanned();
        usleep( 1000 );
    }
    nServerInfo *server = nServerInfo::GetFirstServer();
    while ( server && server->GetPort() != port )
        server = server->Next();
    if ( !server )
    {
        std::cout << "reachable=0 users=0 max_users=0 names=\n";
        tLocale::Clear();
        sn_BasicNetworkSystem.Shutdown();
        return 3;
    }
    server->SetQueryType( nServerInfo::QUERY_ALL );
    server->ClearInfoFlags();
    server->QueryServer();
    for ( int tick = 0; tick < 10000 && !server->Reachable(); ++tick )
    {
        sn_Receive();
        sn_SendPlanned();
        tAdvanceFrame( 1000 );
        usleep( 1000 );
    }

    std::cout << "reachable=" << ( server->Reachable() ? 1 : 0 )
              << " users=" << server->Users()
              << " max_users=" << server->MaxUsers()
              << " names=" << server->UserNames()
              << " combined=" << server->UserNamesOneLine() << "\n";
    int status = server->Reachable() ? 0 : 3;
    nServerInfo::DeleteAll( false );
    tLocale::Clear();
    sn_BasicNetworkSystem.Shutdown();
    return status;
}
