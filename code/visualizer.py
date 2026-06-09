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
    "background": "#F8F9FA",       
    "edge_color": "#7F8C8D",       
    "text_color": "#2C3E50",       
    "border_color": "#BDC3C7",     
    "font_size": 9,
}

# Professional, clean pastel palette
SKILL_PALETTE = {
    "planner": "#FADBD8",     # Soft Pastel Red
    "coder": "#FDEBD0",       # Soft Pastel Orange
    "sandbox_executor": "#E8DAEF", # Soft Pastel Purple
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
            metadata = node.get('metadata')
            if not isinstance(metadata, dict):
                metadata = {}
                
            label_tag = str(metadata.get('label', '')).strip()
            
            # Explicit, deep payload dictionary unpacking for metrics
            result_block = node.get('result') or {}
            elapsed = result_block.get('elapsed_s', 0.0) if isinstance(result_block, dict) else 0.0
            provider = result_block.get('provider', 'N/A') if isinstance(result_block, dict) else 'N/A'

            # Build clean text tokens without emojis to prevent [] font decoding errors
            display_tokens = [f"[{skill.upper()}]"]
            
            if label_tag:
                display_tokens.append(f"Task: {label_tag}")
            else:
                display_tokens.append(f"ID: {node_id}")
                
            if elapsed > 0:
                # Omit 'provider' if it is empty or N/A to keep text clean
                if provider and provider != 'N/A':
                    display_tokens.append(f"Time: {elapsed:.2f}s | {provider}")
                else:
                    display_tokens.append(f"Time: {elapsed:.2f}s")

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
        """Calculates a left-to-right hierarchical flow."""
        try:
            # Assign structural execution layers
            for layer_idx, layer_nodes in enumerate(nx.topological_generations(G)):
                for node in layer_nodes:
                    G.nodes[node]["layer"] = layer_idx
            
            # align="vertical" forces nodes in the same layer to stack vertically, 
            # creating distinct vertical columns that flow left-to-right.
            pos = nx.multipartite_layout(G, subset_key="layer", align="vertical")
            
            # Stretch the X-axis mapping to ensure columns have wide breathing room
            for node in pos:
                pos[node][0] *= 2.5 
            return pos

        except nx.NetworkXUnfeasible:
            logger.warning("Cyclic pipeline detected. Reverting to fallback spring geometry.")
            return nx.spring_layout(G, seed=42)

    def render(self, G: nx.DiGraph, execution_title: str, block: bool = True) -> None:
        """Renders an interactive canvas UI built for asynchronous pipelines."""
        if not G.nodes:
            logger.error("Empty network topology structure. Aborting render phase.")
            return

        fig, ax = plt.subplots(figsize=(16, 9), facecolor=self.theme["background"])
        ax.set_facecolor(self.theme["background"])

        pos = self.compute_dynamic_layout(G)

        # Draw connecting edges underneath nodes (zorder=1)
        nx.draw_networkx_edges(
            G, pos, ax=ax,
            arrowstyle="-|>", arrowsize=20,
            edge_color=self.theme["edge_color"], width=2.0,
            connectionstyle="arc3,rad=0.1",
            node_size=4000, # Pad the arrow end so it stops gracefully before the text box
        )

        # Draw nodes using dynamic Matplotlib text boxes instead of standard NetworkX nodes.
        # This forces the box to perfectly wrap around the text length.
        for node, (x, y) in pos.items():
            color = G.nodes[node]['color']
            label = G.nodes[node]['label']
            
            bbox_props = dict(
                boxstyle="round,pad=0.8", 
                fc=color, 
                ec=self.theme["border_color"], 
                lw=1.5, 
                alpha=0.95
            )
            
            ax.text(
                x, y, label, 
                ha='center', va='center',
                fontsize=self.theme["font_size"], 
                color=self.theme["text_color"],
                fontweight='bold', 
                bbox=bbox_props, 
                zorder=3
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