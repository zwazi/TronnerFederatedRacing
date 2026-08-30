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

#include <cstdlib>
#include <iostream>
#include <unistd.h>

bool *sg_GetSpecs()
{
    static bool spectators[MAXCLIENTS + 2] = {};
    return spectators;
}

int main( int argc, char **argv )
{
    if ( argc != 2 )
        return 2;

    char const *dataDirectory = std::getenv( "TRONNER_ENGINE_DATA_DIR" );
    if ( !dataDirectory || !*dataDirectory )
        return 6;
    tDirectories::SetData( tString( dataDirectory ) );
    tLocale::Load( "languages.txt" );

    nAddress address;
    if ( address.FromString( argv[1] ) != 0 )
        return 4;
    sn_Connect( address );
    if ( sn_GetNetState() != nCLIENT )
        return 3;

    // Join as a real player so the disposable server creates a grid and can
    // transmit the imported cycle back to this client.
    tJUST_CONTROLLED_PTR< ePlayerNetID > local = tNEW(ePlayerNetID)( -1 );
    local->SetName( "Federation Probe" );
    local->SetDefaultTeam();
    local->RequestSync();

    bool foundNamedGhost = false;
    for ( int tick = 0; tick < 2500 && sn_GetNetState() == nCLIENT; ++tick )
    {
        sn_Receive();
        nNetObject::SyncAll();
        sn_SendPlanned();
        for ( int index = 0; index < se_PlayerNetIDs.Len(); ++index )
        {
            ePlayerNetID *player = se_PlayerNetIDs(index);
            if ( player->IsFederationGhost() && player->GetName().Len() > 1 )
            {
                foundNamedGhost = true;
                break;
            }
        }
        if ( foundNamedGhost )
            break;
        usleep( 10000 );
    }

    // Keep the authenticated local player connected long enough for the
    // independent server-info and engine-state probes to inspect its
    // positional pairing with the imported ghost before the arena rotates.
    if ( foundNamedGhost )
        usleep( 8000000 );

    std::cout << "players=" << se_PlayerNetIDs.Len() << "\n";
    for ( int index = 0; index < se_PlayerNetIDs.Len(); ++index )
    {
        ePlayerNetID *player = se_PlayerNetIDs(index);
        std::cout << "id=" << player->ID()
                  << " descriptor=" << player->CreatorDescriptor().ID()
                  << " owner=" << player->Owner()
                  << " human=" << ( player->IsHuman() ? 1 : 0 )
                  << " ai=" << ( dynamic_cast<gAIPlayer *>( player ) ? 1 : 0 )
                  << " ghost=" << ( player->IsFederationGhost() ? 1 : 0 )
                  << " object=" << ( player->Object() ? player->Object()->ID() : 0 )
                  << " alive=" << ( player->Object() && player->Object()->Alive() ? 1 : 0 )
                  << " name=" << player->GetName()
                  << " colored=" << player->GetColoredName()
                  << "\n";
    }

    sn_SetNetState( nSTANDALONE );
    tLocale::Clear();
    return foundNamedGhost ? 0 : 5;
}
