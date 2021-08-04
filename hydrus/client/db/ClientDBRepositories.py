import collections
import itertools
import os
import sqlite3
import typing

from hydrus.core import HydrusConstants as HC
from hydrus.core import HydrusData
from hydrus.core import HydrusDB
from hydrus.core import HydrusDBModule
from hydrus.core import HydrusExceptions
from hydrus.core.networking import HydrusNetwork

from hydrus.client import ClientFiles
from hydrus.client.db import ClientDBDefinitionsCache
from hydrus.client.db import ClientDBFilesMaintenance
from hydrus.client.db import ClientDBFilesMetadataBasic
from hydrus.client.db import ClientDBFilesStorage
from hydrus.client.db import ClientDBServices

def GenerateRepositoryDefinitionTableNames( service_id: int ):
    
    suffix = str( service_id )
    
    hash_id_map_table_name = 'external_master.repository_hash_id_map_{}'.format( suffix )
    tag_id_map_table_name = 'external_master.repository_tag_id_map_{}'.format( suffix )
    
    return ( hash_id_map_table_name, tag_id_map_table_name )
    
def GenerateRepositoryFileDefinitionTableName( service_id: int ):
    
    ( hash_id_map_table_name, tag_id_map_table_name ) = GenerateRepositoryDefinitionTableNames( service_id )
    
    return hash_id_map_table_name
    
def GenerateRepositoryTagDefinitionTableName( service_id: int ):
    
    ( hash_id_map_table_name, tag_id_map_table_name ) = GenerateRepositoryDefinitionTableNames( service_id )
    
    return tag_id_map_table_name
    
def GenerateRepositoryUpdatesTableNames( service_id: int ):
    
    repository_updates_table_name = 'repository_updates_{}'.format( service_id )
    repository_unregistered_updates_table_name = 'repository_unregistered_updates_{}'.format( service_id )
    repository_updates_processed_table_name = 'repository_updates_processed_{}'.format( service_id )
    
    return ( repository_updates_table_name, repository_unregistered_updates_table_name, repository_updates_processed_table_name )
    
class ClientDBRepositories( HydrusDBModule.HydrusDBModule ):
    
    def __init__(
        self,
        cursor: sqlite3.Cursor,
        cursor_transaction_wrapper: HydrusDB.DBCursorTransactionWrapper,
        modules_services: ClientDBServices.ClientDBMasterServices,
        modules_files_storage: ClientDBFilesStorage.ClientDBFilesStorage,
        modules_files_metadata_basic: ClientDBFilesMetadataBasic.ClientDBFilesMetadataBasic,
        modules_hashes_local_cache: ClientDBDefinitionsCache.ClientDBCacheLocalHashes,
        modules_tags_local_cache: ClientDBDefinitionsCache.ClientDBCacheLocalTags,
        modules_files_maintenance: ClientDBFilesMaintenance.ClientDBFilesMaintenance
        ):
        
        # since we'll mostly be talking about hashes and tags we don't have locally, I think we shouldn't use the local caches
        
        HydrusDBModule.HydrusDBModule.__init__( self, 'client repositories', cursor )
        
        self._cursor_transaction_wrapper = cursor_transaction_wrapper
        self.modules_services = modules_services
        self.modules_files_storage = modules_files_storage
        self.modules_files_metadata_basic = modules_files_metadata_basic
        self.modules_files_maintenance = modules_files_maintenance
        self.modules_hashes_local_cache = modules_hashes_local_cache
        self.modules_tags_local_cache = modules_tags_local_cache
        
        self._service_ids_to_content_types_to_outstanding_local_processing = collections.defaultdict( dict )
        
    
    def _ClearOutstandingWorkCache( self, service_id, content_type = None ):
        
        if service_id not in self._service_ids_to_content_types_to_outstanding_local_processing:
            
            return
            
        
        if content_type is None:
            
            del self._service_ids_to_content_types_to_outstanding_local_processing[ service_id ]
            
        else:
            
            if content_type in self._service_ids_to_content_types_to_outstanding_local_processing[ service_id ]:
                
                del self._service_ids_to_content_types_to_outstanding_local_processing[ service_id ][ content_type ]
                
            
        
    
    def _GetInitialIndexGenerationTuples( self ):
        
        index_generation_tuples = []
        
        return index_generation_tuples
        
    
    def _HandleCriticalRepositoryDefinitionError( self, service_id, name, bad_ids ):
        
        self._ReprocessRepository( service_id, ( HC.CONTENT_TYPE_DEFINITIONS, ) )
        
        self._ScheduleRepositoryUpdateFileMaintenance( service_id, ClientFiles.REGENERATE_FILE_DATA_JOB_FILE_INTEGRITY_DATA )
        self._ScheduleRepositoryUpdateFileMaintenance( service_id, ClientFiles.REGENERATE_FILE_DATA_JOB_FILE_METADATA )
        
        self._cursor_transaction_wrapper.CommitAndBegin()
        
        message = 'A critical error was discovered with one of your repositories: its definition reference is in an invalid state. Your repository should now be paused, and all update files have been scheduled for an integrity and metadata check. Please permit file maintenance to check them, or tell it to do so manually, before unpausing your repository. Once unpaused, it will reprocess your definition files and attempt to fill the missing entries. If this error occurs again once that is complete, please inform hydrus dev.'
        message += os.linesep * 2
        message += 'Error: {}: {}'.format( name, bad_ids )
        
        raise Exception( message )
        
    
    def _RegisterUpdates( self, service_id, hash_ids = None ):
        
        ( repository_updates_table_name, repository_unregistered_updates_table_name, repository_updates_processed_table_name ) = GenerateRepositoryUpdatesTableNames( service_id )
        
        if hash_ids is None:
            
            hash_ids = self._STS( self._c.execute( 'SELECT hash_id FROM {};'.format( repository_unregistered_updates_table_name ) ) )
            
        else:
            
            with HydrusDB.TemporaryIntegerTable( self._c, hash_ids, 'hash_id' ) as temp_hash_ids_table_name:
                
                hash_ids = self._STS( self._c.execute( 'SELECT hash_id FROM {} CROSS JOIN {} USING ( hash_id );'.format( temp_hash_ids_table_name, repository_unregistered_updates_table_name ) ) )
                
            
        
        if len( hash_ids ) > 0:
            
            self._ClearOutstandingWorkCache( service_id )
            
            service_type = self.modules_services.GetService( service_id ).GetServiceType()
            
            with HydrusDB.TemporaryIntegerTable( self._c, hash_ids, 'hash_id' ) as temp_hash_ids_table_name:
                
                hash_ids_to_mimes = { hash_id : mime for ( hash_id, mime ) in self._c.execute( 'SELECT hash_id, mime FROM {} CROSS JOIN files_info USING ( hash_id );'.format( temp_hash_ids_table_name ) ) }
                
            
            if len( hash_ids_to_mimes ) > 0:
                
                inserts = []
                processed = False
                
                for ( hash_id, mime ) in hash_ids_to_mimes.items():
                    
                    if mime == HC.APPLICATION_HYDRUS_UPDATE_DEFINITIONS:
                        
                        content_types = ( HC.CONTENT_TYPE_DEFINITIONS, )
                        
                    else:
                        
                        content_types = tuple( HC.REPOSITORY_CONTENT_TYPES[ service_type ] )
                        
                    
                    inserts.extend( ( ( hash_id, content_type, processed ) for content_type in content_types ) )
                    
                
                self._c.executemany( 'INSERT OR IGNORE INTO {} ( hash_id, content_type, processed ) VALUES ( ?, ?, ? );'.format( repository_updates_processed_table_name ), inserts )
                self._c.executemany( 'DELETE FROM {} WHERE hash_id = ?;'.format( repository_unregistered_updates_table_name ), ( ( hash_id, ) for hash_id in hash_ids_to_mimes.keys() ) )
                
            
        
    
    def _ReprocessRepository( self, service_id, content_types ):
        
        ( repository_updates_table_name, repository_unregistered_updates_table_name, repository_updates_processed_table_name ) = GenerateRepositoryUpdatesTableNames( service_id )
        
        self._c.executemany( 'UPDATE {} SET processed = ? WHERE content_type = ?;'.format( repository_updates_processed_table_name ), ( ( False, content_type ) for content_type in content_types ) )
        
        self._ClearOutstandingWorkCache( service_id )
        
    
    def _ScheduleRepositoryUpdateFileMaintenance( self, service_id, job_type ):
        
        ( repository_updates_table_name, repository_unregistered_updates_table_name, repository_updates_processed_table_name ) = GenerateRepositoryUpdatesTableNames( service_id )
        
        table_join = self.modules_files_storage.GetCurrentTableJoinPhrase( self.modules_services.local_update_service_id, repository_updates_table_name )
        
        update_hash_ids = self._STL( self._c.execute( 'SELECT hash_id FROM {};'.format( table_join ) ) )
        
        self.modules_files_maintenance.AddJobs( update_hash_ids, job_type )
        
    
    def AssociateRepositoryUpdateHashes( self, service_key: bytes, metadata_slice: HydrusNetwork.Metadata ):
        
        service_id = self.modules_services.GetServiceId( service_key )
        
        inserts = []
        
        for ( update_index, update_hashes ) in metadata_slice.GetUpdateIndicesAndHashes():
            
            hash_ids = self.modules_hashes_local_cache.GetHashIds( update_hashes )
            
            inserts.extend( ( ( update_index, hash_id ) for hash_id in hash_ids ) )
            
        
        if len( inserts ) > 0:
            
            ( repository_updates_table_name, repository_unregistered_updates_table_name, repository_updates_processed_table_name ) = GenerateRepositoryUpdatesTableNames( service_id )
            
            self._c.executemany( 'INSERT OR IGNORE INTO {} ( update_index, hash_id ) VALUES ( ?, ? );'.format( repository_updates_table_name ), inserts )
            
            self._c.executemany( 'INSERT OR IGNORE INTO {} ( hash_id ) VALUES ( ? );'.format( repository_unregistered_updates_table_name ), ( ( hash_id, ) for ( update_index, hash_id ) in inserts ) )
            
        
        self._RegisterUpdates( service_id )
        
    
    def CreateInitialTables( self ):
        
        pass
        
    
    def DropRepositoryTables( self, service_id: int ):
        
        ( repository_updates_table_name, repository_unregistered_updates_table_name, repository_updates_processed_table_name ) = GenerateRepositoryUpdatesTableNames( service_id )
        
        self._c.execute( 'DROP TABLE IF EXISTS {};'.format( repository_updates_table_name ) )
        self._c.execute( 'DROP TABLE IF EXISTS {};'.format( repository_unregistered_updates_table_name ) )
        self._c.execute( 'DROP TABLE IF EXISTS {};'.format( repository_updates_processed_table_name ) )
        
        ( hash_id_map_table_name, tag_id_map_table_name ) = GenerateRepositoryDefinitionTableNames( service_id )
        
        self._c.execute( 'DROP TABLE IF EXISTS {};'.format( hash_id_map_table_name ) )
        self._c.execute( 'DROP TABLE IF EXISTS {};'.format( tag_id_map_table_name ) )
        
        self._ClearOutstandingWorkCache( service_id )
        
    
    def DoOutstandingUpdateRegistration( self ):
        
        for service_id in self.modules_services.GetServiceIds( HC.REPOSITORIES ):
            
            self._RegisterUpdates( service_id )
            
        
    
    def GenerateRepositoryTables( self, service_id: int ):
        
        ( repository_updates_table_name, repository_unregistered_updates_table_name, repository_updates_processed_table_name ) = GenerateRepositoryUpdatesTableNames( service_id )
        
        self._c.execute( 'CREATE TABLE IF NOT EXISTS {} ( update_index INTEGER, hash_id INTEGER, PRIMARY KEY ( update_index, hash_id ) );'.format( repository_updates_table_name ) )
        self._CreateIndex( repository_updates_table_name, [ 'hash_id' ] )   
        
        self._c.execute( 'CREATE TABLE IF NOT EXISTS {} ( hash_id INTEGER PRIMARY KEY );'.format( repository_unregistered_updates_table_name ) )
        
        self._c.execute( 'CREATE TABLE IF NOT EXISTS {} ( hash_id INTEGER, content_type INTEGER, processed INTEGER_BOOLEAN, PRIMARY KEY ( hash_id, content_type ) );'.format( repository_updates_processed_table_name ) )
        self._CreateIndex( repository_updates_processed_table_name, [ 'content_type' ] )
        
        ( hash_id_map_table_name, tag_id_map_table_name ) = GenerateRepositoryDefinitionTableNames( service_id )
        
        self._c.execute( 'CREATE TABLE IF NOT EXISTS {} ( service_hash_id INTEGER PRIMARY KEY, hash_id INTEGER );'.format( hash_id_map_table_name ) )
        self._c.execute( 'CREATE TABLE IF NOT EXISTS {} ( service_tag_id INTEGER PRIMARY KEY, tag_id INTEGER );'.format( tag_id_map_table_name ) )
        
    
    def GetExpectedTableNames( self ) -> typing.Collection[ str ]:
        
        expected_table_names = [
        ]
        
        return expected_table_names
        
    
    def GetRepositoryProgress( self, service_key: bytes ):
        
        service_id = self.modules_services.GetServiceId( service_key )
        
        ( repository_updates_table_name, repository_unregistered_updates_table_name, repository_updates_processed_table_name ) = GenerateRepositoryUpdatesTableNames( service_id )
        
        ( num_updates, ) = self._c.execute( 'SELECT COUNT( * ) FROM {}'.format( repository_updates_table_name ) ).fetchone()
        
        table_join = self.modules_files_storage.GetCurrentTableJoinPhrase( self.modules_services.local_update_service_id, repository_updates_table_name )
        
        ( num_local_updates, ) = self._c.execute( 'SELECT COUNT( * ) FROM {};'.format( table_join ) ).fetchone()
        
        content_types_to_num_updates = collections.Counter( dict( self._c.execute( 'SELECT content_type, COUNT( * ) FROM {} GROUP BY content_type;'.format( repository_updates_processed_table_name ) ) ) )
        content_types_to_num_processed_updates = collections.Counter( dict( self._c.execute( 'SELECT content_type, COUNT( * ) FROM {} WHERE processed = ? GROUP BY content_type;'.format( repository_updates_processed_table_name ), ( True, ) ) ) )
        
        # little helpful thing that pays off later
        for content_type in content_types_to_num_updates:
            
            if content_type not in content_types_to_num_processed_updates:
                
                content_types_to_num_processed_updates[ content_type ] = 0
                
            
        
        return ( num_local_updates, num_updates, content_types_to_num_processed_updates, content_types_to_num_updates )
        
    
    def GetRepositoryUpdateHashesICanProcess( self, service_key: bytes, content_types_to_process ):
        
        # it is important that we use lists and sort by update index!
        # otherwise add/delete actions can occur in the wrong order
        
        service_id = self.modules_services.GetServiceId( service_key )
        
        ( repository_updates_table_name, repository_unregistered_updates_table_name, repository_updates_processed_table_name ) = GenerateRepositoryUpdatesTableNames( service_id )
        
        result = self._c.execute( 'SELECT 1 FROM {} WHERE content_type = ? AND processed = ?;'.format( repository_updates_processed_table_name ), ( HC.CONTENT_TYPE_DEFINITIONS, True ) ).fetchone()
        
        this_is_first_definitions_work = result is None
        
        result = self._c.execute( 'SELECT 1 FROM {} WHERE content_type != ? AND processed = ?;'.format( repository_updates_processed_table_name ), ( HC.CONTENT_TYPE_DEFINITIONS, True ) ).fetchone()
        
        this_is_first_content_work = result is None
        
        min_unregistered_update_index = None
        
        result = self._c.execute( 'SELECT MIN( update_index ) FROM {} CROSS JOIN {} USING ( hash_id );'.format( repository_unregistered_updates_table_name, repository_updates_table_name ) ).fetchone()
        
        if result is not None:
            
            ( min_unregistered_update_index, ) = result
            
        
        predicate_phrase = 'processed = False AND content_type IN {}'.format( HydrusData.SplayListForDB( content_types_to_process ) )
        
        if min_unregistered_update_index is not None:
            
            # can't process an update if any of its files are as yet unregistered (these are both unprocessed and unavailable)
            # also, we mustn't skip any update indices, so if there is an invalid one, we won't do any after that!
            
            predicate_phrase = '{} AND update_index < {}'.format( predicate_phrase, min_unregistered_update_index )
            
        
        query = 'SELECT update_index, hash_id, content_type FROM {} CROSS JOIN {} USING ( hash_id ) WHERE {};'.format( repository_updates_processed_table_name, repository_updates_table_name, predicate_phrase )
        
        rows = self._c.execute( query ).fetchall()
        
        update_indices_to_unprocessed_hash_ids = HydrusData.BuildKeyToSetDict( ( ( update_index, hash_id ) for ( update_index, hash_id, content_type ) in rows ) )
        hash_ids_to_content_types_to_process = HydrusData.BuildKeyToSetDict( ( ( hash_id, content_type ) for ( update_index, hash_id, content_type ) in rows ) )
        
        all_hash_ids = set( itertools.chain.from_iterable( update_indices_to_unprocessed_hash_ids.values() ) )
        
        all_local_hash_ids = self.modules_files_storage.FilterCurrentHashIds( self.modules_services.local_update_service_id, all_hash_ids )
        
        for sorted_update_index in sorted( update_indices_to_unprocessed_hash_ids.keys() ):
            
            unprocessed_hash_ids = update_indices_to_unprocessed_hash_ids[ sorted_update_index ]
            
            if not unprocessed_hash_ids.issubset( all_local_hash_ids ):
                
                # can't process an update if any of its unprocessed files are not local
                # normally they'll always be available if registered, but just in case a user deletes one manually etc...
                # also, we mustn't skip any update indices, so if there is an invalid one, we won't do any after that!
                
                update_indices_to_unprocessed_hash_ids = { update_index : unprocessed_hash_ids for ( update_index, unprocessed_hash_ids ) in update_indices_to_unprocessed_hash_ids.items() if update_index < sorted_update_index }
                
                break
                
            
        
        # all the hashes are now good to go
        
        all_hash_ids = set( itertools.chain.from_iterable( update_indices_to_unprocessed_hash_ids.values() ) )
        
        hash_ids_to_hashes = self.modules_hashes_local_cache.GetHashIdsToHashes( hash_ids = all_hash_ids )
        
        definition_hashes_and_content_types = []
        content_hashes_and_content_types = []
        
        definitions_content_types = { HC.CONTENT_TYPE_DEFINITIONS }
        
        if len( update_indices_to_unprocessed_hash_ids ) > 0:
            
            for update_index in sorted( update_indices_to_unprocessed_hash_ids.keys() ):
                
                unprocessed_hash_ids = update_indices_to_unprocessed_hash_ids[ update_index ]
                
                definition_hash_ids = { hash_id for hash_id in unprocessed_hash_ids if hash_ids_to_content_types_to_process[ hash_id ] == definitions_content_types }
                content_hash_ids = { hash_id for hash_id in unprocessed_hash_ids if hash_id not in definition_hash_ids }
                
                for ( hash_ids, hashes_and_content_types ) in [
                    ( definition_hash_ids, definition_hashes_and_content_types ),
                    ( content_hash_ids, content_hashes_and_content_types )
                ]:
                    
                    hashes_and_content_types.extend( ( ( hash_ids_to_hashes[ hash_id ], hash_ids_to_content_types_to_process[ hash_id ] ) for hash_id in hash_ids ) )
                    
                
            
        
        return ( this_is_first_definitions_work, definition_hashes_and_content_types, this_is_first_content_work, content_hashes_and_content_types )
        
    
    def GetRepositoryUpdateHashesIDoNotHave( self, service_key: bytes ):
        
        service_id = self.modules_services.GetServiceId( service_key )
        
        ( repository_updates_table_name, repository_unregistered_updates_table_name, repository_updates_processed_table_name ) = GenerateRepositoryUpdatesTableNames( service_id )
        
        all_hash_ids = self._STL( self._c.execute( 'SELECT hash_id FROM {} ORDER BY update_index ASC;'.format( repository_updates_table_name ) ) )
        
        table_join = self.modules_files_storage.GetCurrentTableJoinPhrase( self.modules_services.local_update_service_id, repository_updates_table_name )
        
        existing_hash_ids = self._STS( self._c.execute( 'SELECT hash_id FROM {};'.format( table_join ) ) )
        
        needed_hash_ids = [ hash_id for hash_id in all_hash_ids if hash_id not in existing_hash_ids ]
        
        needed_hashes = self.modules_hashes_local_cache.GetHashes( needed_hash_ids )
        
        return needed_hashes
        
    
    def GetTablesAndColumnsThatUseDefinitions( self, content_type: int ) -> typing.List[ typing.Tuple[ str, str ] ]:
        
        tables_and_columns = []
        
        if HC.CONTENT_TYPE_HASH:
            
            for service_id in self.modules_services.GetServiceIds( HC.REPOSITORIES ):
                
                ( repository_updates_table_name, repository_unregistered_updates_table_name, repository_updates_processed_table_name ) = GenerateRepositoryUpdatesTableNames( service_id )
                hash_id_map_table_name = GenerateRepositoryFileDefinitionTableName( service_id )
                
                tables_and_columns.extend( [
                    ( repository_updates_table_name, 'hash_id' ),
                    ( hash_id_map_table_name, 'hash_id' )
                ] )
                
            
        elif HC.CONTENT_TYPE_TAG:
            
            for service_id in self.modules_services.GetServiceIds( HC.REAL_TAG_SERVICES ):
                
                tag_id_map_table_name = GenerateRepositoryTagDefinitionTableName( service_id )
                
                tables_and_columns.extend( [
                    ( tag_id_map_table_name, 'tag_id' )
                ] )
                
            
        
        return tables_and_columns
        
    
    def HasLotsOfOutstandingLocalProcessing( self, service_id, content_types ):
        
        ( repository_updates_table_name, repository_unregistered_updates_table_name, repository_updates_processed_table_name ) = GenerateRepositoryUpdatesTableNames( service_id )
        
        content_types_to_outstanding_local_processing = self._service_ids_to_content_types_to_outstanding_local_processing[ service_id ]
        
        for content_type in content_types:
            
            if content_type not in content_types_to_outstanding_local_processing:
                
                result = self._STL( self._c.execute( 'SELECT 1 FROM {} WHERE content_type = ? AND processed = ?;'.format( repository_updates_processed_table_name ), ( content_type, False ) ).fetchmany( 20 ) )
                
                content_types_to_outstanding_local_processing[ content_type ] = len( result ) >= 20
                
            
            if content_types_to_outstanding_local_processing[ content_type ]:
                
                return True
                
            
        
        return False
        
    
    def NormaliseServiceHashId( self, service_id: int, service_hash_id: int ) -> int:
        
        hash_id_map_table_name = GenerateRepositoryFileDefinitionTableName( service_id )
        
        result = self._c.execute( 'SELECT hash_id FROM {} WHERE service_hash_id = ?;'.format( hash_id_map_table_name ), ( service_hash_id, ) ).fetchone()
        
        if result is None:
            
            self._HandleCriticalRepositoryDefinitionError( service_id, 'hash_id', service_hash_id )
            
        
        ( hash_id, ) = result
        
        return hash_id
        
    
    def NormaliseServiceHashIds( self, service_id: int, service_hash_ids: typing.Collection[ int ] ) -> typing.Set[ int ]:
        
        hash_id_map_table_name = GenerateRepositoryFileDefinitionTableName( service_id )
        
        with HydrusDB.TemporaryIntegerTable( self._c, service_hash_ids, 'service_hash_id' ) as temp_table_name:
            
            # temp service hashes to lookup
            hash_ids_potentially_dupes = self._STL( self._c.execute( 'SELECT hash_id FROM {} CROSS JOIN {} USING ( service_hash_id );'.format( temp_table_name, hash_id_map_table_name ) ) )
            
        
        # every service_id can only exist once, but technically a hash_id could be mapped to two service_ids
        if len( hash_ids_potentially_dupes ) != len( service_hash_ids ):
            
            bad_service_hash_ids = []
            
            for service_hash_id in service_hash_ids:
                
                result = self._c.execute( 'SELECT hash_id FROM {} WHERE service_hash_id = ?;'.format( hash_id_map_table_name ), ( service_hash_id, ) ).fetchone()
                
                if result is None:
                    
                    bad_service_hash_ids.append( service_hash_id )
                    
                
            
            self._HandleCriticalRepositoryDefinitionError( service_id, 'hash_ids', bad_service_hash_ids )
            
        
        hash_ids = set( hash_ids_potentially_dupes )
        
        return hash_ids
        
    
    def NormaliseServiceTagId( self, service_id: int, service_tag_id: int ) -> int:
        
        tag_id_map_table_name = GenerateRepositoryTagDefinitionTableName( service_id )
        
        result = self._c.execute( 'SELECT tag_id FROM {} WHERE service_tag_id = ?;'.format( tag_id_map_table_name ), ( service_tag_id, ) ).fetchone()
        
        if result is None:
            
            self._HandleCriticalRepositoryDefinitionError( service_id, 'tag_id', service_tag_id )
            
        
        ( tag_id, ) = result
        
        return tag_id
        
    
    def NotifyUpdatesImported( self, hash_ids ):
        
        for service_id in self.modules_services.GetServiceIds( HC.REPOSITORIES ):
            
            self._RegisterUpdates( service_id, hash_ids )
            
        
    
    def ProcessRepositoryDefinitions( self, service_key: bytes, definition_hash: bytes, definition_iterator_dict, content_types, job_key, work_time ):
        
        # ignore content_types for now
        
        service_id = self.modules_services.GetServiceId( service_key )
        
        precise_time_to_stop = HydrusData.GetNowPrecise() + work_time
        
        ( hash_id_map_table_name, tag_id_map_table_name ) = GenerateRepositoryDefinitionTableNames( service_id )
        
        num_rows_processed = 0
        
        if 'service_hash_ids_to_hashes' in definition_iterator_dict:
            
            i = definition_iterator_dict[ 'service_hash_ids_to_hashes' ]
            
            for chunk in HydrusData.SplitIteratorIntoAutothrottledChunks( i, 50, precise_time_to_stop ):
                
                inserts = []
                
                for ( service_hash_id, hash ) in chunk:
                    
                    hash_id = self.modules_hashes_local_cache.GetHashId( hash )
                    
                    inserts.append( ( service_hash_id, hash_id ) )
                    
                
                self._c.executemany( 'REPLACE INTO {} ( service_hash_id, hash_id ) VALUES ( ?, ? );'.format( hash_id_map_table_name ), inserts )
                
                num_rows_processed += len( inserts )
                
                if HydrusData.TimeHasPassedPrecise( precise_time_to_stop ) or job_key.IsCancelled():
                    
                    return num_rows_processed
                    
                
            
            del definition_iterator_dict[ 'service_hash_ids_to_hashes' ]
            
        
        if 'service_tag_ids_to_tags' in definition_iterator_dict:
            
            i = definition_iterator_dict[ 'service_tag_ids_to_tags' ]
            
            for chunk in HydrusData.SplitIteratorIntoAutothrottledChunks( i, 50, precise_time_to_stop ):
                
                inserts = []
                
                for ( service_tag_id, tag ) in chunk:
                    
                    try:
                        
                        tag_id = self.modules_tags_local_cache.GetTagId( tag )
                        
                    except HydrusExceptions.TagSizeException:
                        
                        # in future what we'll do here is assign this id to the 'do not show' table, so we know it exists, but it is knowingly filtered out
                        # _or something_. maybe a small 'invalid' table, so it isn't mixed up with potentially re-addable tags
                        tag_id = self.modules_tags_local_cache.GetTagId( 'invalid repository tag' )
                        
                    
                    inserts.append( ( service_tag_id, tag_id ) )
                    
                
                self._c.executemany( 'REPLACE INTO {} ( service_tag_id, tag_id ) VALUES ( ?, ? );'.format( tag_id_map_table_name ), inserts )
                
                num_rows_processed += len( inserts )
                
                if HydrusData.TimeHasPassedPrecise( precise_time_to_stop ) or job_key.IsCancelled():
                    
                    return num_rows_processed
                    
                
            
            del definition_iterator_dict[ 'service_tag_ids_to_tags' ]
            
        
        self.SetUpdateProcessed( service_id, definition_hash, ( HC.CONTENT_TYPE_DEFINITIONS, ) )
        
        return num_rows_processed
        
    
    def ReprocessRepository( self, service_key: bytes, content_types: typing.Collection[ int ] ):
        
        service_id = self.modules_services.GetServiceId( service_key )
        
        self._ReprocessRepository( service_id, content_types )
        
    
    def ScheduleRepositoryUpdateFileMaintenance( self, service_key, job_type ):
        
        service_id = self.modules_services.GetServiceId( service_key )
        
        self._ScheduleRepositoryUpdateFileMaintenance( service_id, job_type )
        
    
    def SetRepositoryUpdateHashes( self, service_key: bytes, metadata: HydrusNetwork.Metadata ):
        
        service_id = self.modules_services.GetServiceId( service_key )
        
        ( repository_updates_table_name, repository_unregistered_updates_table_name, repository_updates_processed_table_name ) = GenerateRepositoryUpdatesTableNames( service_id )
        
        current_update_hash_ids = self._STS( self._c.execute( 'SELECT hash_id FROM {};'.format( repository_updates_table_name ) ) )
        
        all_future_update_hash_ids = self.modules_hashes_local_cache.GetHashIds( metadata.GetUpdateHashes() )
        
        deletee_hash_ids = current_update_hash_ids.difference( all_future_update_hash_ids )
        
        self._c.executemany( 'DELETE FROM {} WHERE hash_id = ?;'.format( repository_updates_table_name ), ( ( hash_id, ) for hash_id in deletee_hash_ids ) )
        self._c.executemany( 'DELETE FROM {} WHERE hash_id = ?;'.format( repository_unregistered_updates_table_name ), ( ( hash_id, ) for hash_id in deletee_hash_ids ) )
        self._c.executemany( 'DELETE FROM {} WHERE hash_id = ?;'.format( repository_updates_processed_table_name ), ( ( hash_id, ) for hash_id in deletee_hash_ids ) )
        
        inserts = []
        
        for ( update_index, update_hashes ) in metadata.GetUpdateIndicesAndHashes():
            
            for update_hash in update_hashes:
                
                hash_id = self.modules_hashes_local_cache.GetHashId( update_hash )
                
                if hash_id in current_update_hash_ids:
                    
                    self._c.execute( 'UPDATE {} SET update_index = ? WHERE hash_id = ?;'.format( repository_updates_table_name ), ( update_index, hash_id ) )
                    
                else:
                    
                    inserts.append( ( update_index, hash_id ) )
                    
                
            
        
        self._c.executemany( 'INSERT OR IGNORE INTO {} ( update_index, hash_id ) VALUES ( ?, ? );'.format( repository_updates_table_name ), inserts )
        self._c.executemany( 'INSERT OR IGNORE INTO {} ( hash_id ) VALUES ( ? );'.format( repository_unregistered_updates_table_name ), ( ( hash_id, ) for ( update_index, hash_id ) in inserts ) )
        
        self._RegisterUpdates( service_id )
        
        self._ClearOutstandingWorkCache( service_id )
        
    
    def SetUpdateProcessed( self, service_id: int, update_hash: bytes, content_types: typing.Collection[ int ] ):
        
        ( repository_updates_table_name, repository_unregistered_updates_table_name, repository_updates_processed_table_name ) = GenerateRepositoryUpdatesTableNames( service_id )
        
        update_hash_id = self.modules_hashes_local_cache.GetHashId( update_hash )
        
        self._c.executemany( 'UPDATE {} SET processed = ? WHERE hash_id = ? AND content_type = ?;'.format( repository_updates_processed_table_name ), ( ( True, update_hash_id, content_type ) for content_type in content_types ) )
        
        for content_type in content_types:
            
            self._ClearOutstandingWorkCache( service_id, content_type )
            
        
    