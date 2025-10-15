import sys
import json
import logging
import re
from typing import Any, Literal, Optional

import mcp.types as types
from mcp.server.fastmcp import FastMCP
from neo4j import (
    AsyncDriver,
    AsyncGraphDatabase,
    AsyncResult,
    AsyncTransaction
)
from pydantic import Field

logger = logging.getLogger("mcp-genelab")
logger.setLevel(logging.DEBUG)

async def _read(tx: AsyncTransaction, query: str, params: dict[str, Any]) -> str:
    raw_results = await tx.run(query, params)
    eager_results = await raw_results.to_eager_result()

    return json.dumps([r.data() for r in eager_results.records], default=str)

def _is_write_query(query: str) -> bool:
    """Check if the query is a write query."""
    return (
        re.search(r"\b(MERGE|CREATE|SET|DELETE|REMOVE|ADD)\b", query, re.IGNORECASE)
        is not None
    )

def create_mcp_server(neo4j_driver: AsyncDriver, database: str = "neo4j", instructions: str = "") -> FastMCP:
    mcp: FastMCP = FastMCP("mcp-genelab", dependencies=["neo4j", "pydantic"], instructions=instructions)

    async def get_neo4j_schema() -> list[types.TextContent]:
        """List all node, their attributes and their relationships to other nodes in the neo4j database.
        If this fails with a message that includes "Neo.ClientError.Procedure.ProcedureNotFound"
        suggest that the user install and enable the APOC plugin.
        """

        get_schema_query = """
call apoc.meta.data() yield label, property, type, other, unique, index, elementType
where elementType = 'node' and not label starts with '_'
with label, 
    collect(case when type <> 'RELATIONSHIP' then [property, type + case when unique then " unique" else "" end + case when index then " indexed" else "" end] end) as attributes,
    collect(case when type = 'RELATIONSHIP' then [property, head(other)] end) as relationships
RETURN label, apoc.map.fromPairs(attributes) as attributes, apoc.map.fromPairs(relationships) as relationships
"""

        try:
            async with neo4j_driver.session(database=database) as session:
                results_json_str = await session.execute_read(
                    _read, get_schema_query, dict()
                )

                logger.debug(f"Read query returned {len(results_json_str)} rows")
                logger.debug(results_json_str)

                return [types.TextContent(type="text", text=results_json_str)]

        except Exception as e:
            logger.error(f"Database error retrieving schema: {e}")
            return [types.TextContent(type="text", text=f"Error: {e}")]

    async def read_neo4j_cypher(
        query: str = Field(..., description="The Cypher query to execute."),
        params: Optional[dict[str, Any]] = Field(
            None, description="The parameters to pass to the Cypher query."
        ),
    ) -> list[types.TextContent]:
        """Execute a read Cypher query on the neo4j database."""

        if _is_write_query(query):
            raise ValueError("Only MATCH queries are allowed for read-query")

        try:
            async with neo4j_driver.session(database=database) as session:
                results_json_str = await session.execute_read(_read, query, params)

                logger.debug(f"Read query returned {len(results_json_str)} rows")

                return [types.TextContent(type="text", text=results_json_str)]

        except Exception as e:
            logger.error(f"Database error executing query: {e}\n{query}\n{params}")
            return [
                types.TextContent(type="text", text=f"Error: {e}\n{query}\n{params}")
            ]

    async def get_node_metadata() -> list[types.TextContent]:
        """Get metadata for all nodes from MetaNode nodes in the knowledge graph."""

        metadata_query = """
        MATCH (m:MetaNode)
        RETURN m.nodeName as nodeName, m
        ORDER BY m.nodeName
        """

        try:
            async with neo4j_driver.session(database=database) as session:
                results_json_str = await session.execute_read(
                    _read, metadata_query, {}
                )

                logger.debug(f"Metadata query for all nodes returned {len(results_json_str)} characters")

                return [types.TextContent(type="text", text=results_json_str)]

        except Exception as e:
            logger.error(f"Database error retrieving metadata for all nodes: {e}")
            return [types.TextContent(type="text", text=f"Error: {e}")]


    async def get_relationship_metadata() -> list[types.TextContent]:
        """Get descriptions of properties of all relationships in the knowledge graph."""

        metadata_query = """
        MATCH (n1)-[r:MetaRelationship]->(n2)
        WITH n1, r, n2, properties(r) as allProps
        RETURN n1.nodeName as node1, 
               r.relationshipName as relationship, 
               n2.nodeName as node2,
               apoc.map.removeKeys(allProps, ['to', 'from']) AS properties
        """

        try:
            async with neo4j_driver.session(database=database) as session:
                results_json_str = await session.execute_read(
                    _read, metadata_query, {}
                )

                logger.debug(f"Relationship metadata query returned {len(results_json_str)} characters")

                # If MetaRelationship doesn't exist or returns empty, try a fallback approach
                if results_json_str == "[]":
                    logger.debug("MetaRelationship query returned empty, trying fallback to get relationship types")

                    # Get all relationship types in the database
                    fallback_query = """
                    CALL apoc.meta.relTypeProperties()
                    YIELD relType, propertyName, propertyTypes, mandatory
                    WITH relType, collect({propertyName: propertyName,
                                           propertyTypes: propertyTypes}) as properties
                    ORDER BY relType
                    RETURN relType, properties
                    """

                    results_json_str = await session.execute_read(
                        _read, fallback_query, {}
                    )
                    
                    # If that also fails, try using APOC if available
                    if results_json_str == "[]":
                        logger.debug("Basic relationship types query returned empty, trying APOC meta approach")
                        
                        apoc_query = """
                        CALL apoc.meta.graph() YIELD relationships
                        UNWIND relationships as rel
                        WITH rel.type as relType, 
                             keys(rel.properties) as propertyNames,
                             [prop in keys(rel.properties) | rel.properties[prop]] as propertyTypes
                        RETURN relType, propertyNames, propertyTypes
                        ORDER BY relType
                        """
                        
                        results_json_str = await session.execute_read(
                            _read, apoc_query, {}
                        )

                return [types.TextContent(type="text", text=results_json_str)]

        except Exception as e:
            logger.error(f"Database error retrieving relationship metadata: {e}")
            return [types.TextContent(type="text", text=f"Error: {e}")]

    async def find_downregulated_genes(
        study: str = Field(..., description="The study with the assay data (e.g., 'OSD-255')"),
        organism: str = Field(..., description="The organism to search for (e.g., 'Mus musculus')"),
        material: str = Field(..., description="The organ or organ/tissue/cell type to search for (e.g., 'liver')"),
        factor_space_1: str = Field("Ground Control", description="The first experimental grouping of samples using in the assay, indicating whether is was exposed to Space Flight or control conditions. (e.g., 'Ground Control')"),
        factor_space_2: str = Field("Space Flight", description="The second experimental grouping of samples using in the assay, indicating whether is was exposed to Space Flight or control conditions. (e.g., 'Space Flight')"),
        top_n: int = Field(100, description="Number of top downregulated genes to return")
    ) -> list[types.TextContent]:
        """Find downregulated genes for a specific study, organism, and specific organ or tissue.
        
        This tool finds genes that are downregulated for two experimental conditions
        for a specific organism and tissue/organ/cell type. It ensures that only experiments
        with matching factors are compared.
        """

        query = """
        MATCH p = (s:Study)
                  --> (a:Assay)
                  -[m:MEASURED_DIFFERENTIAL_EXPRESSION_ASmMG]-
                  (g:MGene)
        WHERE s.identifier = $study
          AND s.organism = $organism
          AND a.factor_space_1  = $factor_space_1
          AND a.factor_space_2  = $factor_space_2
          AND a.material_name_1 = $material
          AND a.material_name_2 = $material
          AND m.log2fc          < 0

        // strip out the two labels from each factors array
        WITH s, g, m, a,
             [f IN a.factors_1 WHERE NOT f IN [$factor_space_1,$factor_space_2]] AS filtered_factors1,
             [f IN a.factors_2 WHERE NOT f IN [$factor_space_1,$factor_space_2]] AS filtered_factors2

        // only keep rows where the remaining factors match
        WHERE filtered_factors1 = filtered_factors2

        WITH s, g, m, a, filtered_factors1, filtered_factors2
        ORDER BY m.log2fc ASC
        LIMIT $top_n

        RETURN
          'downregulated'      AS regulation,
          s.identifier         AS study,
          s.organism           AS organism,
          g.symbol             AS symbol,
          m.log2fc             AS log2fc,
          m.adj_p_value        AS adj_p_value,
          a.factors_1          AS factors_1,
          a.factors_2          AS factors_2
        """

        params = {
            "study": study,
            "organism": organism,
            "material": material,
            "factor_space_1": factor_space_1,
            "factor_space_2": factor_space_2,
            "top_n": top_n
        }

        try:
            async with neo4j_driver.session(database=database) as session:
                results_json_str = await session.execute_read(_read, query, params)

                logger.debug(f"Find downregulated genes query returned {len(results_json_str)} characters")

                return [types.TextContent(type="text", text=results_json_str)]

        except Exception as e:
            logger.error(f"Database error finding downregulated genes: {e}")
            return [types.TextContent(type="text", text=f"Error: {e}")]

    async def find_upregulated_genes(
        study: str = Field(..., description="The study with the assay data (e.g., 'OSD-255')"),
        organism: str = Field(..., description="The organism to search for (e.g., 'Mus musculus')"),
        material: str = Field(..., description="The organ or organ/tissue/cell type to search for (e.g., 'liver')"),
        factor_space_1: str = Field("Ground Control", description="The first experimental grouping of samples using in the assay, indicating whether is was exposed to Space Flight or control conditions. (e.g., 'Ground Control')"),
        factor_space_2: str = Field("Space Flight", description="The second experimental grouping of samples using in the assay, indicating whether is was exposed to Space Flight or control conditions. (e.g., 'Space Flight')"),
        top_n: int = Field(100, description="Number of top upregulated genes to return")
    ) -> list[types.TextContent]:
        """Find upregulated genes for a specific study, organism, and specific organ or tissue.
        
        This tool finds genes that are upregulated for two experimental conditions
        for a specific organism and tissue/organ/cell type. It ensures that only experiments
        with matching factors are compared.
        """

        query = """
        MATCH p = (s:Study)
                  --> (a:Assay)
                  -[m:MEASURED_DIFFERENTIAL_EXPRESSION_ASmMG]-
                  (g:MGene)
        WHERE s.identifier = $study
          AND s.organism = $organism
          AND a.factor_space_1  = $factor_space_1
          AND a.factor_space_2  = $factor_space_2
          AND a.material_name_1 = $material
          AND a.material_name_2 = $material
          AND m.log2fc          > 0

        // strip out the two labels from each factors array
        WITH s, g, m, a,
             [f IN a.factors_1 WHERE NOT f IN [$factor_space_1,$factor_space_2]] AS filtered_factors1,
             [f IN a.factors_2 WHERE NOT f IN [$factor_space_1,$factor_space_2]] AS filtered_factors2

        // only keep rows where the remaining factors match
        WHERE filtered_factors1 = filtered_factors2

        WITH s, g, m, a, filtered_factors1, filtered_factors2
        ORDER BY m.log2fc DESC
        LIMIT $top_n

        RETURN
          'upregulated'        AS regulation,
          s.identifier         AS study,
          s.organism           AS organism,
          g.symbol             AS symbol,
          m.log2fc             AS log2fc,
          m.adj_p_value        AS adj_p_value,
          a.factors_1          AS factors_1,
          a.factors_2          AS factors_2
        """

        params = {
            "study": study,
            "organism": organism,
            "material": material,
            "factor_space_1": factor_space_1,
            "factor_space_2": factor_space_2,
            "top_n": top_n
        }

        try:
            async with neo4j_driver.session(database=database) as session:
                results_json_str = await session.execute_read(_read, query, params)

                logger.debug(f"Find upregulated genes query returned {len(results_json_str)} characters")

                return [types.TextContent(type="text", text=results_json_str)]

        except Exception as e:
            logger.error(f"Database error finding upregulated genes: {e}")
            return [types.TextContent(type="text", text=f"Error: {e}")]
    
    mcp.add_tool(get_neo4j_schema, name="get_neo4j_schema")
    mcp.add_tool(read_neo4j_cypher, name="read_neo4j_cypher")
    mcp.add_tool(get_node_metadata, name="get_node_metadata")
    mcp.add_tool(get_relationship_metadata, name="get_relationship_metadata")
    mcp.add_tool(find_downregulated_genes, name="find_downregulated_genes")
    mcp.add_tool(find_upregulated_genes, name="find_upregulated_genes")

    return mcp


async def async_main() -> None:
    import os
    db_url = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    username = os.getenv("NEO4J_USERNAME", "neo4j")
    password = os.getenv("NEO4J_PASSWORD", "neo4jdemo")
    database = os.getenv("NEO4J_DATABASE", "spoke-genelab-v0.0.4")
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    instructions = os.getenv("INSTRUCTIONS", "")
    logger.info("Starting mcp-genelab server")

    neo4j_driver = AsyncGraphDatabase.driver(
        db_url,
        auth=(
            username,
            password,
        ),
    )

    mcp = create_mcp_server(neo4j_driver, database, instructions)

    match transport:
        case "stdio":
            await mcp.run_stdio_async()
        case "sse":
            await mcp.run_sse_async()
        case _:
            raise ValueError(f"Invalid transport: {transport} | Must be either 'stdio' or 'sse'")


def main():
    import asyncio
    asyncio.run(async_main())

if __name__ == "__main__":
    main()
