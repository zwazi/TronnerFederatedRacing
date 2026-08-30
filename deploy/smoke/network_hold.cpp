#include "config.h"
#include "defs.h"
#include "nNetwork.h"
#include "nSocket.h"
#include "tLocale.h"
#include "tDirectories.h"
#include "tString.h"

#include <cstdlib>
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
        return 5;
    tDirectories::SetData( tString( dataDirectory ) );
    tLocale::Load( "languages.txt" );
    nAddress address;
    if ( address.FromString( argv[1] ) != 0 )
        return 4;
    sn_Connect( address );
    if ( sn_GetNetState() != nCLIENT )
        return 3;
    for ( int tick = 0; tick < 3000 && sn_GetNetState() == nCLIENT; ++tick )
    {
        sn_Receive();
        sn_SendPlanned();
        usleep( 10000 );
    }
    sn_SetNetState( nSTANDALONE );
    tLocale::Clear();
    return 0;
}
