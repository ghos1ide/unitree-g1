#include <cmath>
#include <cstdlib>
#include <iomanip>
#include <iostream>
#include <set>
#include <stdexcept>
#include <string>
#include <vector>

#include <kdl/treeidsolver_recursive_newton_euler.hpp>
#include <kdl_parser/kdl_parser.hpp>

// Batch fixed-pelvis inverse dynamics. No ROS nodes, DDS or motor commands.
int main(int argc, char** argv)
{
  try {
    KDL::Tree tree;
    if (argc != 2 || !kdl_parser::treeFromFile(argv[1], tree) ||
        tree.getRootSegment()->first != "pelvis") {
      throw std::runtime_error("Expected a URDF tree rooted at pelvis");
    }
    KDL::WrenchMap wrenches;
    for (const auto& segment : tree.getSegments()) {
      wrenches.emplace(segment.first, KDL::Wrench::Zero());
    }
    const auto nj = tree.getNrOfJoints();
    unsigned int cols, rows;
    if (!(std::cin >> cols >> rows) || cols == 0 || cols > nj || rows == 0) {
      throw std::runtime_error("Invalid dynamics batch size");
    }
    std::vector<unsigned int> columns;
    std::set<std::string> seen;
    for (unsigned int i = 0; i < cols; ++i) {
      std::string name;
      std::cin >> name;
      if (!seen.insert(name).second) {
        throw std::runtime_error("Duplicate dynamics joint: " + name);
      }
      bool found = false;
      for (const auto& item : tree.getSegments()) {
        const auto& joint = GetTreeElementSegment(item.second).getJoint();
        if (joint.getName() == name && joint.getType() != KDL::Joint::None) {
          columns.push_back(GetTreeElementQNr(item.second));
          found = true;
          break;
        }
      }
      if (!found) {
        throw std::runtime_error("Dynamics joint not found: " + name);
      }
    }
    KDL::TreeIdSolver_RNE solver(tree, KDL::Vector(0., 0., -9.81));
    KDL::JntArray q(nj), dq(nj), ddq(nj), torque(nj);
    std::cout << std::setprecision(17);
    for (unsigned int row = 0; row < rows; ++row) {
      for (auto* field : {&q, &dq, &ddq}) {
        for (auto column : columns) {
          if (!(std::cin >> (*field)(column)) || !std::isfinite((*field)(column))) {
            throw std::runtime_error("Invalid inverse dynamics input");
          }
        }
      }
      if (solver.CartToJnt(q, dq, ddq, wrenches, torque) < 0) {
        throw std::runtime_error("KDL inverse dynamics failed");
      }
      for (auto column : columns) {
        if (!std::isfinite(torque(column))) {
          throw std::runtime_error("Non-finite inverse dynamics result");
        }
        std::cout << torque(column) << ' ';
      }
      std::cout << '\n';
    }
  } catch (const std::exception& error) {
    std::cerr << error.what() << '\n';
    return EXIT_FAILURE;
  }
  return EXIT_SUCCESS;
}
