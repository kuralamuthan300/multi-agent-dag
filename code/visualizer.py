import json
import argparse
import sys
import logging
from typing import Dict, Any, Tuple
import networkx as nx
import matplotlib.pyplot as plt

# Configure structured logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Light-mode specific visual constraints
THEME_CONFIG = {
    "background": "#F8F9FA",       # Off-white / Soft Light Gray
    "edge_color": "#7F8C8D",       # Slate Gray for connecting lines
    "text_color": "#2C3E50",       # Dark Charcoal for high-contrast legibility
    "border_color": "#BDC3C7",     # Muted Silver for node outlines
    "font_size": 8,
    "node_base_size": 7500,        # Optimized size for multi-line telemetry text
}

# Professional, clean pastel palette optimized for light backdrops
SKILL_PALETTE = {
    "planner": "#FADBD8",     # Soft Pastel Red/Pink
    "researcher": "#D4E6F1",  # Soft Pastel Blue
    "formatter": "#D5F5E3",   # Soft Pastel Green
    "unknown": "#E5E7E9"      # Light Neutral Gray
}

class GraphVisualizer:
    def __init__(self, theme: Dict[str, Any], palette: Dict[str, str]):
        self.theme = theme
        self.palette = palette

    def parse_payload(self, filepath: str) -> Dict[str, Any]:
        """Safely ingests and extracts valid structural nodes and edges."""
        try:
            with open(filepath, 'r') as file:
                return json.load(file)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            logger.error(f"Failed to process target pipeline file: {str(e)}")
            sys.exit(1)

    def build_graph(self, data: Dict[str, Any]) -> nx.DiGraph:
        """Constructs a networkx DiGraph injecting comprehensive telemetry attributes."""
        G = nx.DiGraph()

        for node in data.get('nodes', []):
            node_id = node.get('id')
            if not node_id:
                continue

            skill = node.get('skill', 'unknown')
            status = node.get('status', 'unknown')
            
            # Safe extraction of metadata dictionary
            metadata = node.get('metadata')
            if not isinstance(metadata, dict):
                metadata = {}
                
            # Strip whitespace to catch completely empty string labels safely
            label_tag = str(metadata.get('label', '')).strip()
            
            # Explicit, deep payload dictionary unpacking for metrics
            result_block = node.get('result') or {}
            elapsed = result_block.get('elapsed_s', 0.0) if isinstance(result_block, dict) else 0.0
            provider = result_block.get('provider', 'N/A') if isinstance(result_block, dict) else 'N/A'

            # Build unified token label with clean status indicators
            status_symbol = "🔵" if status == "complete" else "⚪"
            
            # Core Node Identification header
            node_header = f"{status_symbol} {skill.upper()}"
            
            # FIX: Structural assignment rule to prevent trailing/empty brackets []
            if label_tag:
                identity_line = f"[{label_tag}]"
            else:
                identity_line = f"ID: {node_id}"
                
            display_tokens = [node_header, identity_line]
                
            # Append execution metric line only if a valid runtime duration exists
            if elapsed > 0:
                display_tokens.append(f"⏱️ {elapsed:.2f}s | {provider}")

            G.add_node(
                node_id,
                label="\n".join(display_tokens),
                color=self.palette.get(skill, self.palette["unknown"]),
                skill=skill
            )

        for edge in data.get('edges', []):
            src, tgt = edge.get('source'), edge.get('target')
            if src and tgt and src in G and tgt in G:
                G.add_edge(src, tgt)

        return G

    def compute_dynamic_layout(self, G: nx.DiGraph) -> Dict[Any, Tuple[float, float]]:
        """Calculates structural layout positions using dynamic scaling metrics."""
        try:
            # Assign structural execution layers
            for layer_idx, layer_nodes in enumerate(nx.topological_generations(G)):
                for node in layer_nodes:
                    G.nodes[node]["layer"] = layer_idx
            
            pos = nx.multipartite_layout(G, subset_key="layer", align="horizontal")
            
            # Prevent visual collision by adjusting scale based on node counts
            layer_counts = [G.nodes[n].get('layer', 0) for n in G.nodes]
            max_density = max(layer_counts.count(i) for i in set(layer_counts)) if layer_counts else 1
            
            # Stretch aspect ratio to prevent labels from bleeding together
            horizontal_factor = max(2.2, max_density * 0.45)
            for node in pos:
                pos[node][0] *= horizontal_factor
            return pos

        except nx.NetworkXUnfeasible:
            logger.warning("Cyclic pipeline detected. Reverting calculation to fallback spring geometry.")
            return nx.spring_layout(G, k=2.0 / (len(G.nodes) ** 0.5), seed=42)

    def render(self, G: nx.DiGraph, execution_title: str, block: bool = True) -> None:
        """Renders an interactive canvas UI built for asynchronous pipelines."""
        if not G.nodes:
            logger.error("Empty network topology structure. Aborting render phase.")
            return

        fig, ax = plt.subplots(figsize=(16, 9), facecolor=self.theme["background"])
        ax.set_facecolor(self.theme["background"])

        pos = self.compute_dynamic_layout(G)
        labels = nx.get_node_attributes(G, 'label')
        colors = [node_attr['color'] for _, node_attr in G.nodes(data=True)]

        # Render connection channels using clean dark gray lines and larger arrows
        nx.draw_networkx_edges(
            G, pos, ax=ax,
            arrowstyle="-|>", arrowsize=20,
            edge_color=self.theme["edge_color"], width=1.8,
            connectionstyle="arc3,rad=0.08"
        )

        # Render primary nodes with crisp borders
        nx.draw_networkx_nodes(
            G, pos, ax=ax,
            node_color=colors, node_size=self.theme["node_base_size"],
            node_shape="s", alpha=1.0,
            edgecolors=self.theme["border_color"], linewidths=1.5
        )

        # Inject high-contrast dark text layers on top of the pastel nodes
        nx.draw_networkx_labels(
            G, pos, labels=labels,
            font_size=self.theme["font_size"], font_weight='bold',
            font_color=self.theme["text_color"], ax=ax
        )

        plt.title(execution_title, fontsize=16, color=self.theme["text_color"], pad=20, fontweight='bold')
        
        # Strip structural canvas ticks
        for spine in ax.spines.values():
            spine.set_visible(False)

        plt.margins(0.15)
        plt.tight_layout()
        
        plt.show(block=block)

def main():
    parser = argparse.ArgumentParser(description="Visualize a planner graph from a JSON file.")
    parser.add_argument("filepath", help="Path to the JSON file containing the graph data")
    parser.add_argument("--non-blocking", action="store_true", help="Prevent the UI thread from blocking")
    args = parser.parse_args()

    engine = GraphVisualizer(theme=THEME_CONFIG, palette=SKILL_PALETTE)
    raw_data = engine.parse_payload(args.filepath)
    execution_graph = engine.build_graph(raw_data)
    
    engine.render(
        execution_graph, 
        execution_title=f"Execution Pipeline Topology: {args.filepath}",
        block=not args.non_blocking
    )

if __name__ == "__main__":
    main()