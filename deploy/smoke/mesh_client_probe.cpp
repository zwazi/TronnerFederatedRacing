#include "config.h"
#include "defs.h"
#include "eNetGameObject.h"
#include "ePlayer.h"
#include "gAIBase.h"
#include "nNetwork.h"
#include "nSocket.h"
#include "tDirectories.h"
#include "tLocale.h"
#include "tString.h"

#include <chrono>
#include <cstdlib>
#include <iostream>
#include <string>
#include <unistd.h>

bool *sg_GetSpecs()
{
    static bool spectators[MAXCLIENTS + 2] = {};
    return spectators;
}

namespace
{
using Clock = std::chrono::steady_clock;

long ElapsedMilliseconds( Clock::time_point started )
{
    return std::chrono::duration_cast<std::chrono::milliseconds>(
        Clock::now() - started
    ).count();
}

void Milestone( Clock::time_point started, char const *phase )
{
    std::cout << "PROBE phase=" << phase
              << " elapsed_ms=" << ElapsedMilliseconds( started ) << "\n";
}
}

int main( int argc, char **argv )
{
    if ( argc < 2 || argc > 5 )
    {
        std::cerr << "usage: mesh_client_probe host:port [observer|player] "
                     "[settle-milliseconds] [chat-command]\n";
        return 2;
    }

    std::string mode = argc >= 3 ? argv[2] : "observer";
    if ( mode != "observer" && mode != "player" )
        return 2;
    long settleMilliseconds = argc >= 4 ? std::atol( argv[3] ) : 2500;
    if ( settleMilliseconds < 250 || settleMilliseconds > 30000 )
        return 2;
    std::string chatCommand = argc >= 5 ? argv[4] : "";
    if (
        !chatCommand.empty()
        && ( chatCommand[0] != '/' || chatCommand.size() > 128 )
    )
        return 2;

    char const *dataDirectory = std::getenv( "TRONNER_ENGINE_DATA_DIR" );
    if ( !dataDirectory || !*dataDirectory )
        return 6;
    tDirectories::SetData( tString( dataDirectory ) );
    tLocale::Load( "languages.txt" );

    Clock::time_point started = Clock::now();
    Milestone( started, "starting" );
    nAddress address;
    if ( address.FromString( argv[1] ) != 0 )
        return 4;
    sn_Connect( address );
    if ( sn_GetNetState() != nCLIENT )
        return 3;
    Milestone( started, "connect_requested" );

    tJUST_CONTROLLED_PTR< ePlayerNetID > local = tNEW(ePlayerNetID)( -1 );
    local->SetName( "MeshProbe" );
    if ( mode == "player" )
        local->SetDefaultTeam();
    local->RequestSync();

    bool loginReported = false;
    bool commandSent = false;
    bool objectReported = false;
    long loginMilliseconds = -1;
    long maximumMilliseconds = settleMilliseconds + 15000;
    while (
        ElapsedMilliseconds( started ) < maximumMilliseconds
        && sn_GetNetState() == nCLIENT
    )
    {
        sn_Receive();
        nNetObject::SyncAll();
        sn_SendPlanned();
        if ( !loginReported && local->Owner() > 0 )
        {
            loginReported = true;
            loginMilliseconds = ElapsedMilliseconds( started );
            Milestone( started, "login_succeeded" );
        }
        if ( loginReported && !commandSent && !chatCommand.empty() )
        {
            local->Chat( tString( chatCommand.c_str() ) );
            commandSent = true;
            Milestone( started, "command_sent" );
        }
        if ( !objectReported && local->Object() )
        {
            objectReported = true;
            Milestone( started, "cycle_received" );
        }
        if (
            loginReported
            && ElapsedMilliseconds( started ) >=
                loginMilliseconds + settleMilliseconds
        )
            break;
        usleep( 10000 );
    }

    std::cout << "PROBE phase=state"
              << " elapsed_ms=" << ElapsedMilliseconds( started )
              << " mode=" << mode
              << " logged_in=" << ( loginReported ? 1 : 0 )
              << " local_owner=" << local->Owner()
              << " local_object=" << ( local->Object() ? local->Object()->ID() : 0 )
              << " local_alive="
              << ( local->Object() && local->Object()->Alive() ? 1 : 0 )
              << " players=" << se_PlayerNetIDs.Len() << "\n";
    for ( int index = 0; index < se_PlayerNetIDs.Len(); ++index )
    {
        ePlayerNetID *player = se_PlayerNetIDs(index);
        std::cout << "PROBE phase=player"
                  << " id=" << player->ID()
                  << " owner=" << player->Owner()
                  << " human=" << ( player->IsHuman() ? 1 : 0 )
                  << " ai=" << ( dynamic_cast<gAIPlayer *>( player ) ? 1 : 0 )
                  << " ghost=" << ( player->IsFederationGhost() ? 1 : 0 )
                  << " object=" << ( player->Object() ? player->Object()->ID() : 0 )
                  << " alive="
                  << ( player->Object() && player->Object()->Alive() ? 1 : 0 )
                  << " name=" << player->GetName() << "\n";
    }

    sn_SetNetState( nSTANDALONE );
    tLocale::Clear();
    Milestone( started, "finished" );
    return loginReported ? 0 : 5;
}
